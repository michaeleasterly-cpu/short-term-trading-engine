"""Unit tests for the deployed ``ops.lane_service`` daemon — slimmed to
DETERMINISTIC SELF-HEAL ONLY (2026-05-22).

Operator directive 2026-05-21 ("we wont be deploying the llm data
triage it will run locally with my max account"): the deployed
``lane-service`` daemon hosts ONLY the deterministic ``data_repair``
co-task. The three previous LLM-invoking co-tasks (``triage_data`` /
``triage_engine`` / ``triage_lab_emitter``) are REMOVED from the
deployed daemon and now run OPERATOR-LOCALLY via slash skills.

Coverage:
  (a) Module surface: ``LANE_NAMES`` is exactly ``("data_repair",)``
      (the deterministic co-task; no triage co-tasks here).
  (b) ``POOL_MAX_SIZE`` accommodates the single co-task plus headroom.
  (c) ``_run_supervised`` mirrors the engine-service crash-isolation
      contract: a non-Cancelled exception is logged and the lane is
      restarted after backoff; CancelledError propagates.
  (d) The lane factory delegates to the SOURCE module's main loop
      (``ops.data_repair_service._main_loop``).
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


def test_lane_names_exactly_data_repair_and_operator_trigger(
    lane_service_mod,
) -> None:
    """Behaviour (a): the deployed daemon hosts exactly TWO
    deterministic co-tasks:

      * ``data_repair`` — polls ENGINE_DATA_REQUEST events
        (existing, 2026-05-21).
      * ``operator_trigger`` — polls OPERATOR_RUN_REQUESTED events
        written by the console-api when the operator clicks Run
        data update / Run validation / Run feed (added 2026-05-29
        for the build_real_data_pipeline_operations_console task).

    Both are deterministic, no LLM, no autonomous fallback. The
    three previous LLM-invoking co-tasks remain REMOVED (operator
    directive 2026-05-22) — neither one of these new entries calls
    Anthropic.

    The two-daemon Railway invariant (engine-service + lane-service
    + data-operations cron) is preserved — we added a co-task to
    ``lane_service``, not a new daemon."""
    assert lane_service_mod.LANE_NAMES == (
        "data_repair", "operator_trigger",
    )


def test_pool_max_size_at_least_one_per_lane(lane_service_mod) -> None:
    """Behaviour (b): pool sized ≥ one connection per co-task plus
    headroom for in-flight repair acquires."""
    assert lane_service_mod.POOL_MAX_SIZE >= len(lane_service_mod.LANE_NAMES)


@pytest.mark.asyncio
async def test_run_supervised_restarts_on_non_cancelled_exception(
    lane_service_mod,
) -> None:
    """Behaviour (c): a non-Cancelled exception is caught, logged, and
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
    """Behaviour (c): CancelledError propagates (clean shutdown
    semantics — the supervisor must NOT swallow it)."""
    stop_event = asyncio.Event()

    async def factory():
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await lane_service_mod._run_supervised(
            "cancel_lane", factory, stop_event, backoff=0.01,
        )


def test_factory_delegates_to_data_repair_main_loop(lane_service_mod) -> None:
    """Behaviour (d): the deployed daemon's lane factory imports the
    SAME main loop the (now retired) standalone data-repair-service
    used. Asserting via the imported symbol identity — no behavioural
    rewrite of the deterministic lane."""
    from ops.data_repair_service import _main_loop as src_data_repair

    # The orchestrator's imported name (visible via module globals)
    # is bound to the same callable the source module exposes.
    assert lane_service_mod._data_repair_main_loop is src_data_repair


def test_deployed_daemon_does_not_import_llm_triage_modules(
    lane_service_mod,
) -> None:
    """Operator directive 2026-05-21: the deployed daemon must NOT pull
    any LLM-invoking module into the deployed process. This asserts
    structurally that ``ops.lane_service``'s globals do NOT carry any
    binding from the LLM-triage source modules (the four prior co-task
    factories that pulled ``ops.llm_triage_service`` /
    ``ops.llm_data_recovery`` / ``ops.engine_llm_triage`` /
    ``ops.llm_lab_emitter`` are gone)."""
    forbidden_names = {
        "_triage_data_main_loop",
        "_triage_engine_loop",
        "_triage_lab_emitter_loop",
        "run_autonomous_recovery",
        "engine_run_triage",
        "run_lab_emitter_cotask",
    }
    leaked = forbidden_names & set(vars(lane_service_mod).keys())
    assert not leaked, (
        f"LLM-triage symbols leaked into the deployed daemon: {leaked}. "
        "Per operator directive 2026-05-21 the deployed daemon must run "
        "deterministic-only; LLM invocation is operator-local."
    )
