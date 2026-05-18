"""SDLC SP2 T9 — binding zero-live-side-effects isolation test (H-S2-6).

Lives in ``tpcore/tests`` (a COLLECTED pyproject testpath), NOT
``tpcore/lab/tests`` (uncollected — a safety test there would silently
never run). DB-gated: skips locally with no ``DATABASE_URL``; CI has one
and runs it fully (``asyncio_mode = auto`` — no decorator needed).

This composes the shipped pieces end-to-end: a REAL Lab walk-forward run
(``ops.lab.run.amain`` inside an active ``LabContext``, candidate set)
MUST produce zero row-delta on every live-write table and persist the
credibility rubric ONLY under the Lab-namespaced source — never the live
engine's. The read pool MUST reject writes (L1 floor) and every guarded
live-side-effect class MUST raise inside an active Lab run (L3 floor).

API-alignment note: the plan's T9 sketch calls
``LabRun(candidate=..., ...).execute()``. No ``LabRun`` class exists yet
(T10 wires the CLI/class); T5 extracted the walk-forward into
``ops.lab.run.amain(args, candidate=None)``. Per the plan's own T9
instruction, this drives the REAL existing entrypoint — the asserted
invariants are kept verbatim, only the call is aligned (mirrors the
``_NS`` arg-namespace the T1/T6 characterization oracle builds).
"""
import os
import uuid
from datetime import date

import pytest

from tpcore.backtest.credibility import CREDIBILITY_SOURCE_PREFIX

# Derived source strings — mirrors T6's test_lab_no_gate_poison.py pinning
# so there is zero drift if the prefix or format ever changes.
#
# NOTE: we do NOT import ops.lab.run at module level here. ops.lab.run is
# an ops/ *package* module; importing it at collection time clobbers
# sys.modules["ops"], which breaks test_ops.py — that test inserts
# scripts/ into sys.path and does `import ops` to reach scripts/ops.py
# (a different "ops"). _lab_credibility_engine_name is a pure one-liner
# f"lab.{candidate}"; we inline the same format so the collection import
# stays light while remaining drift-safe via the shared
# CREDIBILITY_SOURCE_PREFIX constant.
_REV_SOURCE = f"{CREDIBILITY_SOURCE_PREFIX}.reversion"
_LAB_SOURCE = f"{CREDIBILITY_SOURCE_PREFIX}.lab.iso_probe"

pytestmark = pytest.mark.skipif(
    os.environ.get("DATABASE_URL") is None,
    reason="Lab isolation test needs a DB (CI has DATABASE_URL; "
    "local skips by design — do NOT force it locally, CI runs it)",
)


async def _rowcount(pool, table, where=""):
    async with pool.acquire() as c:
        return await c.fetchval(f"SELECT count(*) FROM platform.{table} {where}")


class _LabArgs:
    """Mirrors the real ``ops.lab.run._parse_args`` argparse Namespace
    fields (see ``_parse_args`` in ops/lab/run.py) — tiny + permissive so
    the real reversion walk-forward stays light. universe_tier_max=None ⇒
    the engine's built-in default mega-cap universe (no tier query). The
    assertions are about ZERO live-write deltas, not about a SURVIVED
    verdict, so thresholds are floored and the window is short."""

    engine = "reversion"
    trials = 2
    per_window_trials = 1
    train_start = date(2022, 1, 1)
    holdout_end = date(2023, 12, 31)
    final_holdout_start = date(2024, 1, 1)
    final_holdout_end = date(2024, 12, 31)
    walk_forward_step = 365
    train_years = 1
    holdout_years = 1
    seed = 0
    output = None
    database_url = None  # ⇒ amain falls back to $DATABASE_URL
    dsr_threshold = 0.0
    credibility_threshold = 0
    universe_tier_max = None


async def test_lab_run_zero_live_side_effects(tmp_path):
    """A real Lab run (``amain`` inside ``LabContext``, candidate set,
    target reversion) asserts:

    BINDING SAFETY INVARIANTS (UNCONDITIONAL — the real contract, true
    regardless of whether the search produced a rankable/persist result):
    - risk_state / open_orders / aar_events row-count == before (zero live-write)
    - application_log STARTUP row-count == before
    - live reversion credibility source row-count == before (H-S2-3 no-poison)
    - lab-namespaced source row-count >= before (never decreases — re-run-safe)

    POSITIVE END-TO-END PROOF (rc==0-gated — SURVIVED ⇒ persist ran):
    - lab_rows >= lab_before + 1 only when rc==0 because rc==1 means
      FAILED-or-not-ranked and write_credibility_score may not have run;
      asserting +1 on rc==1 would be a false-wolf that gets this safety
      net disabled on thin CI data. The ABSENCE of poison is the real
      contract and is asserted unconditionally above.

    (H-S2-3 no-poison + H-S2-6 zero-side-effect, end-to-end)
    """
    import ops.lab.run as lab_run
    from tpcore.db import build_asyncpg_pool
    from tpcore.lab.context import LabContext

    url = os.environ["DATABASE_URL"]
    # Plain RW audit pool (NO read_only) — snapshots, not under test.
    audit = await build_asyncpg_pool(url, max_size=1)
    try:
        before = {
            t: await _rowcount(audit, t)
            for t in ("risk_state", "open_orders", "aar_events")
        }
        startup_before = await _rowcount(
            audit, "application_log", "WHERE event_type='STARTUP'")
        rev_before = await _rowcount(
            audit, "data_quality_log",
            f"WHERE source='{_REV_SOURCE}'")
        lab_before = await _rowcount(
            audit, "data_quality_log",
            f"WHERE source='{_LAB_SOURCE}'")

        args = _LabArgs()
        args.output = tmp_path / "iso_probe_results.csv"
        async with LabContext(db_url=url):
            # Real existing entrypoint:
            #   async def amain(args, candidate=None) -> int
            # candidate="iso_probe" ⇒ credibility persists under
            # backtest_credibility.lab.iso_probe (the H-S2-3 seam).
            rc = await lab_run.amain(args, candidate="iso_probe")
        assert rc in (0, 1), f"unexpected amain rc={rc}"

        # ── BINDING SAFETY INVARIANTS (UNCONDITIONAL) ─────────────────
        # Zero live-write deltas — true regardless of search outcome.
        for t, b in before.items():
            assert await _rowcount(audit, t) == b, f"Lab wrote platform.{t}"
        assert await _rowcount(
            audit, "application_log",
            "WHERE event_type='STARTUP'") == startup_before, \
            "Lab emitted a STARTUP application_log row"

        # H-S2-3 no-poison: live reversion source must be byte-identical.
        assert await _rowcount(
            audit, "data_quality_log",
            f"WHERE source='{_REV_SOURCE}'") == rev_before, \
            f"Lab poisoned {_REV_SOURCE}"

        # Lab-namespaced source must never decrease (re-run-safe).
        lab_rows = await _rowcount(
            audit, "data_quality_log",
            f"WHERE source='{_LAB_SOURCE}'")
        assert lab_rows >= lab_before, (
            f"Lab-namespaced source DECREASED: {_LAB_SOURCE} "
            f"(before={lab_before}, after={lab_rows})"
        )

        # ── POSITIVE END-TO-END PROOF (rc==0-gated) ───────────────────
        # rc==0 (SURVIVED) ⇒ the held-back run completed AND
        # write_credibility_score ran — the namespaced row MUST exist.
        # rc==1 (FAILED or not-ranked) ⇒ persist may not have occurred
        # (early-exit paths: `not ranked`, credibility_rubric is None) —
        # skip the +1 check to avoid false-wolf on thin CI data; the
        # binding safety invariants above still hold and are asserted.
        if rc == 0:
            assert lab_rows >= lab_before + 1, (
                f"SURVIVED run must have persisted exactly under the "
                f"lab-namespaced source {_LAB_SOURCE} "
                f"(before={lab_before}, after={lab_rows})"
            )
    finally:
        await audit.close()


async def test_read_pool_rejects_write_and_guards_fire():
    """L1 floor: a write through ``LabContext.read_pool`` raises
    asyncpg ReadOnlySQLTransactionError. L3 floor: constructing each
    guarded live-side-effect class inside an active LabContext raises
    LabIsolationViolation (DBLogHandler's guard is in startup(), not
    __init__ — its __init__ rejects pool=None, so it gets a real pool)."""
    import asyncpg

    from tpcore.db import build_asyncpg_pool
    from tpcore.lab.context import LabContext, LabIsolationViolation

    url = os.environ["DATABASE_URL"]
    async with LabContext(db_url=url) as lc:
        # ── L1: read pool rejects writes server-side ──────────────────
        with pytest.raises(asyncpg.exceptions.ReadOnlySQLTransactionError):
            async with lc.read_pool.acquire() as c:
                await c.execute(
                    "CREATE TEMP TABLE _lab_iso_probe(x int); "
                    "INSERT INTO _lab_iso_probe VALUES (1)")

        # ── L3: every guarded constructor fires inside the Lab ────────
        from tpcore.risk.governor import RiskGovernor
        with pytest.raises(LabIsolationViolation):
            RiskGovernor(None, None)  # guard before any arg use

        from tpcore.aar.writer import AARWriter
        with pytest.raises(LabIsolationViolation):
            AARWriter(None)

        from tpcore.order_management.base_order_manager import (
            BaseOrderManager,
        )
        with pytest.raises(LabIsolationViolation):
            BaseOrderManager(
                broker=None, governor=None, capital_gate=None,
                lifecycle=None, aar=None)

        from tpcore.alpaca.broker_adapter import AlpacaPaperBrokerAdapter
        with pytest.raises(LabIsolationViolation):
            AlpacaPaperBrokerAdapter()

        # DBLogHandler.__init__ rejects pool=None (ValueError) before the
        # guard could run, so the guard lives in startup(). Build a real
        # pool, construct cleanly, then assert startup() raises. The pool
        # is built read-only — startup() never reaches a write because the
        # guard short-circuits first (this is exactly the point).
        from tpcore.logging.db_handler import DBLogHandler
        guard_pool = await build_asyncpg_pool(url, read_only=True, max_size=1)
        try:
            handler = DBLogHandler(guard_pool, "x", uuid.uuid4())
            with pytest.raises(LabIsolationViolation):
                await handler.startup()
        finally:
            await guard_pool.close()
