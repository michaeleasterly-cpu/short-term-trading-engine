"""Autonomous LLM edge-finder agent — Task #25 §3.2 Phase A/B/C orchestration.

The agent ties every layer together:
- T1 contracts (frozen pydantic v2 schemas)
- T2 reference loader (mandatory + named bundles)
- T4 snapshot assembler (Phase A)
- T5 tool sandbox (Phase B dispatch)
- T6 persona (system-prompt content)
- T7 provenance writer (LAB_FINDER_RUN + LAB_FINDER_ACTION rows)

Phase D/E/F (auto-promote / outcome monitor / auto-retire) live in
separate modules (T8 covers Phase A-C orchestration only; T9 adds the
Anthropic SDK; T10 adds the co-task daemon; T11+ ship operator surfaces).

This module is the in-process loop: `run_finder(...)` is awaitable +
returns `FinderRun`. The LLM call site is a seam (`_call_llm`) that
T9 implements against the Anthropic SDK; tests inject a fake.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from datetime import date as date_t
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

import structlog

from tpcore.lab.llm_finder import (
    ANALYSIS_TURN_QUOTA,
    EDGE_FINDER_RUN_QUOTA,
    MANDATORY_REFERENCE_BUNDLES,
    PERSONA_VERSION,
)
from tpcore.lab.llm_finder.models import (
    AnalysisRequest,
    AnalysisResult,
    FinderRun,
    MarketSnapshot,
    ProposedSpec,
    ToolCall,
    ToolResult,
)
from tpcore.lab.llm_finder.persona import persona_text
from tpcore.lab.llm_finder.reference_loader import (
    ReferenceExcerpt,
    load_reference_bundles,
)
from tpcore.lab.llm_finder.run_writer import record_finder_run
from tpcore.lab.llm_finder.snapshot import assemble_snapshot
from tpcore.lab.llm_finder.tool_sandbox import dispatch

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

log = structlog.get_logger(__name__)

# LLM seam — T9 wires the Anthropic SDK; tests inject a fake.
LLMCallable = Callable[
    [str, str, list[dict[str, Any]]],  # system_prompt, user_prompt_first_turn, transcript
    Awaitable[dict[str, Any]],
]


class AgentError(RuntimeError):
    """Agent encountered an unrecoverable error; FinderRun.rejection_reason carries detail."""


# ───────────────────────── Top-level runner ─────────────────────────


async def run_finder(
    pool: asyncpg.Pool,
    *,
    trigger: Literal[
        "operator_command",
        "ledger_capacity_event",
        "regime_change_event",
    ],
    session_date: date_t,
    target_engine: str | None = None,
    extra_reference_bundles: tuple[str, ...] = (),
    llm_callable: LLMCallable | None = None,
) -> FinderRun:
    """Run the Phase A/B/C loop end-to-end + persist FinderRun.

    Args:
        pool: asyncpg connection pool for snapshot + provenance writes.
        trigger: which event fired this run (provenance §2.16).
        session_date: snapshot's session_date (typically `today` minus 1).
        target_engine: optional override; if None, the LLM picks from roster.
        extra_reference_bundles: caller-requested non-mandatory bundles.
        llm_callable: LLM seam; T9 binds the Anthropic SDK; tests inject fake.
            None ⇒ the agent runs Phase A only + emits an empty
            AnalysisResult (the smoke-mode path).

    Returns:
        FinderRun with run_id + emissions + provenance.
    """
    run_id = uuid4()
    started_ts = datetime.now(UTC)
    log.info(
        "finder.run.start",
        run_id=str(run_id),
        trigger=trigger,
        session_date=str(session_date),
    )

    # ── Phase A: snapshot + references ──────────────────────────────
    snapshot = await assemble_snapshot(pool, session_date=session_date)
    bundles = load_reference_bundles(names=extra_reference_bundles)
    reference_bundle_names = tuple(b.name for b in bundles)

    # ── Phase B + C: LLM-driven analysis loop + emission ──────────
    if llm_callable is not None:
        result, tool_results = await _drive_llm_loop(
            llm_callable=llm_callable,
            snapshot=snapshot,
            bundles=bundles,
            target_engine=target_engine,
        )
    else:
        result, tool_results = _empty_result(), ()

    # Truncate at the run quota (defense-in-depth on top of the pydantic cap).
    capped_specs = _truncate_specs(result.proposed_specs)

    completed_ts = datetime.now(UTC)
    run = FinderRun(
        run_id=run_id,
        started_ts=started_ts,
        completed_ts=completed_ts,
        trigger=trigger,
        snapshot_session_date=session_date,
        snapshot_regime_tuple_id=snapshot.market_regime.regime_tuple_id,
        persona_version=PERSONA_VERSION,
        reference_bundles=reference_bundle_names,
        analysis_turn_count=_count_turns(tool_results),
        proposed_spec_count=len(capped_specs),
        emitted_pr_urls=(),
        auto_merged_pr_urls=(),
        auto_issued_ecr_refs=(),
        rejection_reason=None,
    )
    await record_finder_run(pool, run)
    log.info(
        "finder.run.complete",
        run_id=str(run_id),
        emissions=len(capped_specs),
        turns=run.analysis_turn_count,
    )
    return run


# ───────────────────────── Phase B+C loop driver ─────────────────────────


async def _drive_llm_loop(
    *,
    llm_callable: LLMCallable,
    snapshot: MarketSnapshot,
    bundles: tuple[ReferenceExcerpt, ...],
    target_engine: str | None,
) -> tuple[AnalysisResult, tuple[ToolResult, ...]]:
    """Run the bounded LLM ↔ tool-sandbox loop.

    Loop terminates on:
    - LLM emits an AnalysisResult ('kind' == 'AnalysisResult' in
      the decoded JSON envelope).
    - ANALYSIS_TURN_QUOTA reached without an AnalysisResult.

    Each turn:
    - The LLM sees: system_prompt (persona), user_prompt (snapshot
      summary + bundles + transcript), AND a directive to either emit
      another AnalysisRequest (more analysis turns needed) OR emit
      the final AnalysisResult (specs ready).
    - The agent decodes the JSON envelope, runs each tool_call through
      tool_sandbox.dispatch(), appends results to the transcript, and
      either loops or terminates.
    """
    system_prompt = persona_text()
    user_prompt = _compose_user_prompt(snapshot, bundles, target_engine)
    transcript: list[dict[str, Any]] = []
    tool_results_accumulated: list[ToolResult] = []

    for turn in range(1, ANALYSIS_TURN_QUOTA + 1):
        envelope = await llm_callable(system_prompt, user_prompt, transcript)
        decoded = _decode_llm_response(envelope, turn)

        if decoded["kind"] == "AnalysisResult":
            result = AnalysisResult(
                tool_results=tuple(tool_results_accumulated),
                proposed_specs=tuple(
                    ProposedSpec(**s) for s in decoded.get("proposed_specs", ())
                ),
                finder_rationale=decoded.get("finder_rationale", "(empty)"),
            )
            return result, tuple(tool_results_accumulated)

        # AnalysisRequest → dispatch all tool_calls + extend transcript.
        # LLM natural shape: {"tool": "X", "params": {...}} OR
        # {"name": "X", "args": {...}}. Normalize to ToolCall's
        # {"callable_name": "X", "args_json": "..."}.
        request = AnalysisRequest(
            turn=turn,
            rationale=decoded.get("rationale", "(empty)"),
            tool_calls=tuple(
                ToolCall(**_normalize_tool_call(c))
                for c in decoded.get("tool_calls", ())
            ),
        )
        turn_results: list[ToolResult] = []
        for call in request.tool_calls:
            turn_results.append(dispatch(call, snapshot))
        tool_results_accumulated.extend(turn_results)
        transcript.append({
            "turn": turn,
            "rationale": request.rationale,
            "tool_calls": [c.model_dump() for c in request.tool_calls],
            "tool_results": [r.model_dump() for r in turn_results],
        })

    # Quota exhausted without an emission — return empty result.
    log.warning(
        "finder.analysis.quota_exhausted",
        turns=ANALYSIS_TURN_QUOTA,
    )
    return (
        AnalysisResult(
            tool_results=tuple(tool_results_accumulated),
            proposed_specs=(),
            finder_rationale="quota_exhausted_no_emission",
        ),
        tuple(tool_results_accumulated),
    )


# ───────────────────────── Helpers ─────────────────────────


def _empty_result() -> AnalysisResult:
    return AnalysisResult(
        tool_results=(),
        proposed_specs=(),
        finder_rationale="(no llm; smoke-mode)",
    )


def _truncate_specs(specs: tuple[ProposedSpec, ...]) -> tuple[ProposedSpec, ...]:
    """Cap at EDGE_FINDER_RUN_QUOTA with loud warning per spec §10.2."""
    if len(specs) <= EDGE_FINDER_RUN_QUOTA:
        return specs
    log.warning(
        "finder.run_quota.truncated",
        emitted_count=len(specs),
        kept=EDGE_FINDER_RUN_QUOTA,
    )
    return specs[:EDGE_FINDER_RUN_QUOTA]


def _count_turns(tool_results: tuple[ToolResult, ...]) -> int:
    """Best-effort turn count from the accumulated tool results."""
    # We don't carry turn-id on ToolResult; use len/MAX_TOOL_CALLS_PER_TURN
    # as an approximate ceiling. Conservative defaults to len/4.
    if not tool_results:
        return 0
    return max(1, (len(tool_results) + 3) // 4)


def _compose_user_prompt(
    snapshot: MarketSnapshot,
    bundles: tuple[ReferenceExcerpt, ...],
    target_engine: str | None,
) -> str:
    """Build the first-turn user prompt; subsequent turns extend via transcript."""
    bundle_blocks = "\n\n".join(
        f"# Reference: {b.name}\n\n{b.content}" for b in bundles
    )
    snapshot_summary = (
        f"## MarketSnapshot (session_date={snapshot.session_date})\n\n"
        f"- regime: {snapshot.market_regime.model_dump_json()}\n"
        f"- universe: {snapshot.universe}\n"
        f"- price_window: {len(snapshot.price_window)} rows\n"
        f"- fundamentals: {len(snapshot.fundamentals)} rows\n"
        f"- spreads: {len(snapshot.spreads)} rows\n"
        f"- sentiment: {len(snapshot.sentiment)} rows\n"
        f"- macro: {len(snapshot.macro)} rows\n"
        f"- ledger_state: {len(snapshot.ledger_state)} rows\n"
        f"- roster: {[r.engine for r in snapshot.roster]}\n"
    )
    target_directive = (
        f"Target engine: {target_engine}\n" if target_engine else
        "Target engine: select from snapshot.roster (your choice).\n"
    )
    return (
        "You are the autonomous LLM edge-finder.\n\n"
        f"{snapshot_summary}\n\n"
        f"{target_directive}\n\n"
        "# Reference bundles (mandatory + caller-requested)\n\n"
        f"{bundle_blocks}\n\n"
        "# Output contract\n\n"
        "Respond ONLY with a JSON envelope. Either:\n"
        "  {'kind': 'AnalysisRequest', 'rationale': '...', 'tool_calls': [{...}, ...]}\n"
        "OR (when ready to emit):\n"
        "  {'kind': 'AnalysisResult', 'proposed_specs': [{...}, ...], 'finder_rationale': '...'}\n"
        "Refer to the persona §7 workflow for what each phase requires."
    )


def _normalize_tool_call(call_dict: dict[str, Any]) -> dict[str, Any]:
    """Translate the LLM's natural shape into ToolCall's pydantic schema.

    LLM often emits {"tool": "X", "params": {...}} or {"name": "X",
    "args": {...}} instead of the spec's {"callable_name": "X",
    "args_json": "..."}. This normalizer accepts both.
    """
    import json as _json
    if "callable_name" in call_dict and "args_json" in call_dict:
        return call_dict  # already canonical
    name = (
        call_dict.get("callable_name")
        or call_dict.get("tool")
        or call_dict.get("name")
        or ""
    )
    args = (
        call_dict.get("args_json")
        or call_dict.get("params")
        or call_dict.get("args")
        or {}
    )
    if not isinstance(args, str):
        args = _json.dumps(args)
    return {"callable_name": name, "args_json": args}


def _decode_llm_response(envelope: dict[str, Any], turn: int) -> dict[str, Any]:
    """Validate the LLM's JSON envelope shape."""
    if not isinstance(envelope, dict):
        raise AgentError(f"turn {turn}: envelope not a dict")
    kind = envelope.get("kind")
    if kind not in ("AnalysisRequest", "AnalysisResult"):
        raise AgentError(f"turn {turn}: kind='{kind}' not in (AnalysisRequest, AnalysisResult)")
    return envelope


# Default mandatory bundles used by callers needing the canonical set.
DEFAULT_REFERENCE_BUNDLES = MANDATORY_REFERENCE_BUNDLES


# ───────────────────────── Co-task entry point (T10) ─────────────────────────

# Per spec §3.4: the daemon polls application_log for these event classes.
# v1 keeps the set EMPTY by design (mirror SP-G's original posture):
# - LAB_LEDGER_CAPACITY_AVAILABLE: requires SP-A ledger-decay emitter (separate PR)
# - REGIME_CHANGE_OBSERVED: requires a regime-classifier event emitter (separate PR)
# Until those emitters ship, the co-task is structurally present but its
# trigger set is empty — the operator-command path (the /lab-edge-find slash
# skill in T11) is the v1 trigger.
EDGE_FINDER_TRIGGER_EVENT_TYPES: tuple[str, ...] = ()


async def run_edge_finder_cotask(pool: asyncpg.Pool, trigger_event: Any) -> None:
    """Co-task entry — invoked by the llm_triage_service daemon.

    Called when an event in EDGE_FINDER_TRIGGER_EVENT_TYPES is observed on
    application_log. Wraps run_finder() with a default trigger derived from
    the event class. The LLM seam is the production Anthropic SDK callable
    (T9). This function is the daemon ↔ finder boundary.

    Per spec §3.4 + .claude/rules/llm-triage.md:
    - Event-driven only (NOT scheduled).
    - Advisory; no `tools` param.
    - Draft-PR only (Phase D auto-promote ships in a follow-up PR).
    - Crash-isolated (raises propagate to _run_supervised, which restarts
      this co-task on backoff — sibling co-tasks unaffected).
    """
    # Default to current UTC session_date; the real triggers will carry
    # session_date in their payloads (event-emitter PR).
    from datetime import datetime

    from ops.llm_edge_finder_sdk import AuthSkip, make_sdk_llm_callable

    trigger_class: Literal[
        "operator_command",
        "ledger_capacity_event",
        "regime_change_event",
    ]
    event_type = (
        trigger_event.get("event_type", "") if isinstance(trigger_event, dict)
        else getattr(trigger_event, "event_type", "")
    )
    if event_type == "LAB_LEDGER_CAPACITY_AVAILABLE":
        trigger_class = "ledger_capacity_event"
    elif event_type == "REGIME_CHANGE_OBSERVED":
        trigger_class = "regime_change_event"
    else:
        trigger_class = "operator_command"

    session_date = datetime.now(UTC).date()
    try:
        llm_callable = make_sdk_llm_callable()
    except Exception as exc:  # noqa: BLE001 - degrade to smoke mode
        log.warning("edge_finder_cotask.sdk_init_failed", error=str(exc))
        llm_callable = None

    try:
        await run_finder(
            pool,
            trigger=trigger_class,
            session_date=session_date,
            llm_callable=llm_callable,
        )
    except AuthSkip:
        log.warning("edge_finder_cotask.auth_skip", note="no ANTHROPIC_API_KEY")
        # Re-run in smoke mode (no LLM) so the provenance row still lands.
        await run_finder(
            pool,
            trigger=trigger_class,
            session_date=session_date,
            llm_callable=None,
        )


__all__ = [
    "DEFAULT_REFERENCE_BUNDLES",
    "EDGE_FINDER_TRIGGER_EVENT_TYPES",
    "AgentError",
    "LLMCallable",
    "run_edge_finder_cotask",
    "run_finder",
]


# ───────────────────────── CLI entry point (T11) ─────────────────────────


async def _amain(argv: list[str]) -> int:
    """`python -m ops.llm_edge_finder` entry point — invoked by `/lab-edge-find`."""
    import argparse
    import asyncio  # noqa: F401 - imported for the running loop's context
    import os
    from datetime import datetime as _datetime

    from tpcore.db import build_asyncpg_pool

    parser = argparse.ArgumentParser(
        prog="ops.llm_edge_finder",
        description="Task #25 Path B autonomous LLM edge-finder (one run).",
    )
    parser.add_argument(
        "--trigger",
        choices=("operator_command", "ledger_capacity_event", "regime_change_event"),
        default="operator_command",
    )
    parser.add_argument(
        "--target",
        help="Optional target engine; if omitted, the LLM picks from snapshot.roster.",
    )
    parser.add_argument(
        "--reference-bundle",
        default="",
        help="Comma-separated list of caller-named bundles to include alongside the 3 mandatory bundles.",
    )
    args = parser.parse_args(argv)

    dsn = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_IPV4")
    if not dsn:
        log.error("edge_finder.no_dsn", note="set DATABASE_URL or DATABASE_URL_IPV4")
        return 1

    pool = await build_asyncpg_pool(dsn, max_size=2, read_only=True)
    try:
        extra_bundles: tuple[str, ...] = tuple(
            b.strip() for b in args.reference_bundle.split(",") if b.strip()
        )
        # Build the LLM callable lazily so AuthSkip degrades cleanly.
        llm_callable: LLMCallable | None
        try:
            from ops.llm_edge_finder_sdk import make_sdk_llm_callable
            llm_callable = make_sdk_llm_callable()
        except Exception as exc:  # noqa: BLE001
            log.warning("edge_finder.sdk_unavailable", error=str(exc))
            llm_callable = None

        run = await run_finder(
            pool,
            trigger=args.trigger,
            session_date=_datetime.now(UTC).date(),
            target_engine=args.target,
            extra_reference_bundles=extra_bundles,
            llm_callable=llm_callable,
        )
        log.info(
            "edge_finder.cli.done",
            run_id=str(run.run_id),
            emissions=run.proposed_spec_count,
            trigger=args.trigger,
        )
        return 0
    finally:
        await pool.close()


def main() -> None:  # pragma: no cover - CLI shim
    import asyncio
    import sys
    sys.exit(asyncio.run(_amain(sys.argv[1:])))


if __name__ == "__main__":  # pragma: no cover
    main()
