"""SDLC SP2 T9 — binding zero-live-side-effects isolation test (H-S2-6).

Lives in ``tpcore/tests`` (a COLLECTED pyproject testpath), NOT
``tpcore/lab/tests`` (uncollected — a safety test there would silently
never run). DB-gated: skips with no ``DATABASE_URL`` (``asyncio_mode =
auto`` — no decorator needed). This DB gate is INHERITED from SP2 and the
repo's ``.github/workflows/ci.yml`` ``test`` job has NO ``services:
postgres`` / ``DATABASE_URL`` — so this suite skips in automated CI
exactly as it skips locally. The make-or-break SP-A proofs
(``test_cumulative_n_trials_real_db_integer_correctness`` [H-LL-9],
``test_lab_ledger_disjoint_from_live_graduation`` [T-LIVE/H-LL-4]) are
NOT exercised by CI on merge; they are enforced at merge time by the
mandatory operator-run compensating control (the recorded
``DATABASE_URL=$DATABASE_URL_IPV4 .venv/bin/python -m pytest
tpcore/tests/test_lab_isolation.py -q`` run against the live Postgres).
A dedicated scoped CI-Postgres job is a tracked TODO.md follow-up — see
spec H-LL-9 / TODO.md "Lab-isolation DB proofs not CI-enforced".

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
    reason="Lab isolation test needs a DB. This DB gate is INHERITED "
    "from SP2; the repo's ci.yml `test` job has NO Postgres service / "
    "DATABASE_URL, so this skips in CI exactly as it skips locally. "
    "The make-or-break proofs are enforced at merge by the mandatory "
    "operator-run compensating control (see spec H-LL-9 / TODO.md "
    "'Lab-isolation DB proofs not CI-enforced'), NOT by automated CI.",
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


async def test_lab_ledger_disjoint_from_live_graduation(tmp_path):
    """MAKE-OR-BREAK · T-LIVE. A real Lab walk-forward run targeting the
    LIVE engine ``reversion`` (candidate set ⇒ Lab path) writes a
    ``lab_trial_ledger.reversion`` spend row, yet:

    - ``graduation_ready(pool, "reversion")`` is byte-identical to
      before (the ledger source is invisible to the live gate),
    - the live ``backtest_credibility.reversion`` source row-count is
      unchanged (H-S2-3 no-poison, re-asserted alongside the ledger),
    - ``graduation_ready``'s SQL filters strictly on
      ``backtest_credibility.{engine}`` — it never reads
      ``lab_trial_ledger.*`` (namespace disjointness, H-LL-4),
    - the ledger DID record the spend (so the disjointness is
      *meaningful*, not vacuous: a row exists and is still invisible).
    """
    import inspect

    import ops.lab.run as lab_run
    from tpcore.backtest.credibility import (
        CREDIBILITY_SOURCE_PREFIX,
        graduation_ready,
    )
    from tpcore.db import build_asyncpg_pool
    from tpcore.lab.context import LabContext
    from tpcore.lab.ledger import ledger_source

    # Static disjointness: graduation_ready reads ONLY the credibility
    # prefix; the ledger source uses a different prefix entirely.
    grad_src = inspect.getsource(graduation_ready)
    assert f"{CREDIBILITY_SOURCE_PREFIX}." in grad_src
    assert "lab_trial_ledger" not in grad_src
    assert not ledger_source("reversion").startswith(
        f"{CREDIBILITY_SOURCE_PREFIX}.")

    url = os.environ["DATABASE_URL"]
    audit = await build_asyncpg_pool(url, max_size=1)
    try:
        rev_cred_src = f"{CREDIBILITY_SOURCE_PREFIX}.reversion"
        ledger_src = ledger_source("reversion")
        rev_cred_before = await _rowcount(
            audit, "data_quality_log", f"WHERE source='{rev_cred_src}'")
        ledger_before = await _rowcount(
            audit, "data_quality_log", f"WHERE source='{ledger_src}'")
        grad_before = await graduation_ready(audit, "reversion")

        args = _LabArgs()
        args.output = tmp_path / "ledger_iso.csv"
        async with LabContext(db_url=url):
            rc = await lab_run.amain(args, candidate="ledger_iso_probe")
        assert rc in (0, 1), f"unexpected amain rc={rc}"

        # Live gate read byte-identical.
        assert await graduation_ready(audit, "reversion") == grad_before, \
            "Lab run changed graduation_ready(reversion) — live poison"
        # Live credibility source row-count unchanged (no-poison).
        assert await _rowcount(
            audit, "data_quality_log",
            f"WHERE source='{rev_cred_src}'") == rev_cred_before, \
            f"Lab poisoned {rev_cred_src}"
        # The ledger DID record the spend (disjointness is meaningful).
        ledger_after = await _rowcount(
            audit, "data_quality_log", f"WHERE source='{ledger_src}'")
        assert ledger_after >= ledger_before + 1, (
            f"Lab run must have emitted a {ledger_src} spend row "
            f"(before={ledger_before}, after={ledger_after})")
    finally:
        await audit.close()


async def test_cumulative_n_trials_real_db_integer_correctness():
    """MAKE-OR-BREAK (H-LL-9). Seed KNOWN lab_trial_ledger rows in the
    real DB and assert cumulative_n_trials returns the EXACT integer —
    pins the live SQL: SUM (not COUNT), the notes::jsonb->>'trials'
    int-cast (not another key), the source=$1 per-target predicate
    (cross-target isolation), and the strict ``timestamp < before_ts``
    boundary. None of these are observable through the offline fakes.
    """
    from datetime import UTC, datetime

    from tpcore.db import build_asyncpg_pool
    from tpcore.lab.ledger import cumulative_n_trials, record_trial_spend

    url = os.environ["DATABASE_URL"]
    pool = await build_asyncpg_pool(url, max_size=1)
    try:
        # Unique throwaway targets so the assertion is independent of any
        # prior ledger history in this DB (the SUM is monotone/append-only;
        # uuid mirrors the file's existing uuid4 usage).
        tgt = f"revtest_{uuid.uuid4().hex[:12]}"
        other = f"vectest_{uuid.uuid4().hex[:12]}"
        base = await cumulative_n_trials(pool, tgt, datetime.now(UTC))
        assert base == 0, f"fresh target must be 0, got {base}"

        # Seed the 3 INCLUDED rows FIRST. record_trial_spend stamps a
        # server-side datetime.now(UTC) and returns that exact ts.
        await record_trial_spend(
            pool, target=tgt, candidate="h-ll-9", trials=40, seed=1)
        await record_trial_spend(
            pool, target=tgt, candidate="h-ll-9", trials=60, seed=2)
        # Cross-target row that must NOT be summed into `tgt` (source=$1).
        await record_trial_spend(
            pool, target=other, candidate="h-ll-9", trials=999, seed=9)
        # cutoff captured AFTER the 3 included rows and BEFORE the
        # excluded write — so the trials=7 row's returned ts is strictly
        # GREATER than cutoff (causal: capture-then-write, not now()-racy).
        cutoff = datetime.now(UTC)
        # Strictly-after row: its ts > cutoff ⇒ excluded by `< cutoff`.
        spend7_ts = await record_trial_spend(
            pool, target=tgt, candidate="h-ll-9", trials=7, seed=3)

        got = await cumulative_n_trials(pool, tgt, cutoff)
        assert got == 100, (
            f"cumulative SUM wrong: expected 40+60=100 (cross-target "
            f"{other}=999 excluded by source predicate; the trials=7 "
            f"post-cutoff row excluded by strict '<'), got {got} — "
            f"a SQL-text regression (JSON key / SUM / predicate / "
            f"boundary) in cumulative_n_trials")
        # Cross-target isolation, asserted from the other side too.
        assert await cumulative_n_trials(pool, other, cutoff) == 999
        # GENUINE `<` vs `<=` discriminator: a row exists EXACTLY at
        # spend7_ts. With strict `<` that row is EXCLUDED ⇒ 100; a
        # `<`→`<=` regression would INCLUDE it ⇒ 107.
        assert await cumulative_n_trials(pool, tgt, spend7_ts) == 100
    finally:
        await pool.close()
