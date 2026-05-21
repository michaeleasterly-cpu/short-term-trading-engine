"""Unit tests for the consolidated ``ops.lane_service`` daemon (2026-05-21).

The 2-daemon Railway-budget fix: the previous data-repair-service and
llm-triage-service are fused into ONE daemon hosting FOUR co-tasks
under one ``asyncio.gather()``. Both source modules
(``ops.data_repair_service`` / ``ops.llm_triage_service``) remain
intact as importable libraries — lane_service is a thin orchestrator.

Coverage:
  (a) Module surface: ``LANE_NAMES`` is exactly the four lanes the
      docstring + design pin (``data_repair``, ``triage_data``,
      ``triage_engine``, ``triage_lab_emitter``).
  (b) ``POOL_MAX_SIZE`` accommodates one acquire per co-task plus
      headroom.
  (c) The two lock dirs stay DISTINCT (different mutual-exclusion
      domains: data-ops lock vs triage lock).
  (d) ``_run_supervised`` mirrors the engine-service crash-isolation
      contract: a non-Cancelled exception is logged and the lane is
      restarted after backoff; CancelledError propagates.
  (e) The lane factories delegate to the SOURCE modules' main loops
      (no behavioural rewrite of the lanes — the orchestrator only
      composes).
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

# Load via importlib (parity with the precedent in
# tests/test_llm_triage_service.py — ops.lane_service does intra-
# package imports, but pytest's sys.path layout normally resolves
# ``ops`` as a package. We stick with the direct import here; if
# collection order ever shadows it the test will fail loudly and
# point at the same `ops`-package-shadow root cause as its siblings).

# Pin to one worker — this is part of the ops package-shadow family.
pytestmark = pytest.mark.xdist_group("ops_shadow")


_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))


@pytest.fixture
def lane_service_mod():
    """Reload-safe import of ops.lane_service."""
    spec = importlib.util.spec_from_file_location(
        "_lane_service_under_test",
        _REPO / "ops" / "lane_service.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_lane_service_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_lane_names_exactly_four(lane_service_mod) -> None:
    """Behaviour (a): the four co-tasks the daemon hosts."""
    assert lane_service_mod.LANE_NAMES == (
        "data_repair",
        "triage_data",
        "triage_engine",
        "triage_lab_emitter",
    )


def test_pool_max_size_at_least_one_per_lane(lane_service_mod) -> None:
    """Behaviour (b): pool sized ≥ one connection per co-task plus
    headroom for in-flight repair/triage acquires."""
    assert lane_service_mod.POOL_MAX_SIZE >= len(lane_service_mod.LANE_NAMES)


def test_lock_dirs_are_distinct(lane_service_mod) -> None:
    """Behaviour (c): the data-ops lock and the triage lock are
    DIFFERENT directories — they guard different mutual-exclusion
    domains (vs run_data_operations.sh vs ad-hoc
    `python -m ops.llm_triage_service`)."""
    from ops.data_repair_service import DEFAULT_LOCK_DIR as DR
    from ops.llm_triage_service import DEFAULT_LOCK_DIR as TR
    assert DR != TR


@pytest.mark.asyncio
async def test_run_supervised_restarts_on_non_cancelled_exception(
    lane_service_mod,
) -> None:
    """Behaviour (d): a non-Cancelled exception is caught, logged, and
    the lane is restarted after backoff. Sets the stop_event on the
    second attempt to keep the test bounded."""
    attempts = {"n": 0}
    stop_event = asyncio.Event()

    async def factory():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("simulated lane crash")
        # second attempt: clean exit + return
        stop_event.set()

    await lane_service_mod._run_supervised(
        "test_lane", factory, stop_event, backoff=0.01,
    )
    # First attempt failed; supervisor restarted; second attempt
    # set stop_event and returned cleanly. The supervisor must have
    # been called AT LEAST twice (failure + restart).
    assert attempts["n"] >= 2


@pytest.mark.asyncio
async def test_run_supervised_propagates_cancellederror(
    lane_service_mod,
) -> None:
    """Behaviour (d): CancelledError propagates (clean shutdown
    semantics — the supervisor must NOT swallow it)."""
    stop_event = asyncio.Event()

    async def factory():
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await lane_service_mod._run_supervised(
            "cancel_lane", factory, stop_event, backoff=0.01,
        )


def test_factories_delegate_to_source_modules(lane_service_mod) -> None:
    """Behaviour (e): the lane factories import the SAME main loops
    that the (now retired) standalone daemons used. Asserting via the
    imported symbol identities — no behavioural rewrite."""
    from ops.data_repair_service import _main_loop as src_data_repair
    from ops.llm_triage_service import _engine_loop as src_triage_engine
    from ops.llm_triage_service import (
        _lab_emitter_loop as src_triage_lab_emitter,
    )
    from ops.llm_triage_service import _main_loop as src_triage_data

    # The orchestrator's imported names (visible via module globals)
    # are bound to the same callables the source modules expose.
    assert lane_service_mod._data_repair_main_loop is src_data_repair
    assert lane_service_mod._triage_data_main_loop is src_triage_data
    assert lane_service_mod._triage_engine_loop is src_triage_engine
    assert lane_service_mod._triage_lab_emitter_loop is src_triage_lab_emitter
