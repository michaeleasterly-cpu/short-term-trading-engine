"""Autonomous LLM-AAR critic — spec §11 main loop.

The critic ties every layer together:
- tpcore.lab.llm_aar.models (frozen pydantic v2 schemas)
- tpcore.lab.llm_aar.payload_assembler (deterministic AAR aggregates)
- tpcore.lab.llm_aar.persona (system prompt text)
- tpcore.lab.llm_aar.run_writer (provenance writers)
- tpcore.lab.llm_aar.memstore_writer (best-effort memstore archival)

Mirrors ops/llm_edge_finder.py shape: in-process loop, awaitable
``run_aar_critic(...)``, LLM call site is a seam (`LLMCallable`) that
the SDK wires to the Anthropic Sessions API; tests inject a fake.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from datetime import date as date_t
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

import structlog
from pydantic import ValidationError

from tpcore.lab.llm_aar import (
    MAX_FINDINGS_PER_ENGINE_PER_RUN,
    PERSONA_VERSION,
)
from tpcore.lab.llm_aar.models import (
    AARCriticRun,
    AARFinding,
    EnginePerformanceWindow,
    compute_finding_id,
)
from tpcore.lab.llm_aar.memstore_writer import (
    archive_finding_to_aar_memstore,
    copy_finding_to_finder_memstore,
)
from tpcore.lab.llm_aar.payload_assembler import assemble_aar_payload
from tpcore.lab.llm_aar.persona import persona_text
from tpcore.lab.llm_aar.run_writer import record_aar_critic_run, record_aar_finding

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg
    from anthropic import AsyncAnthropic

log = structlog.get_logger(__name__)


# LLM seam — the SDK wires the Anthropic Sessions API; tests inject a fake.
LLMCallable = Callable[
    [str, str, list[dict[str, Any]]],  # system_prompt, user_prompt_first_turn, transcript
    Awaitable[dict[str, Any]],
]


class CriticError(RuntimeError):
    """Unrecoverable critic error; AARCriticRun.rejection_reason carries detail."""


# ───────────────────────── Top-level runner ─────────────────────────


async def run_aar_critic(
    pool: asyncpg.Pool,
    *,
    trigger: Literal["nightly_cron", "operator_command"],
    as_of_session: date_t,
    llm_callable: LLMCallable | None = None,
    anthropic_client: AsyncAnthropic | None = None,
    aar_memstore_id: str | None = None,
    finder_memstore_id: str | None = None,
) -> AARCriticRun:
    """Run the AAR-critic loop end-to-end + persist AARCriticRun.

    Args:
        pool: asyncpg connection pool for AAR reads + provenance writes.
        trigger: which event fired this run (provenance).
        as_of_session: anchor session date; the 90-session window runs
            backwards from here.
        llm_callable: LLM seam; the SDK binds the Anthropic Sessions API.
            None ⇒ smoke mode (payload assembled + provenance written;
            no findings emitted).
        anthropic_client: optional AsyncAnthropic instance used for memstore
            archival (best-effort). None ⇒ memstore writes are skipped
            (application_log row remains the binding record).
        aar_memstore_id: AAR critic dedicated memstore (spec §4.1). When set
            + ``anthropic_client`` provided, each finding lands at
            ``/findings/<engine>/<finding_id>.md``.
        finder_memstore_id: finder memstore for curated copy (spec §4.3).
            When set + ``anthropic_client`` provided, each finding ALSO
            lands in the finder memstore at
            ``/aar-findings/<engine>/<finding_id>.md``.

    Returns:
        AARCriticRun with run_id + emissions + provenance.
    """
    run_id = uuid4()
    started_ts = datetime.now(UTC)
    log.info(
        "aar_critic.run.start",
        run_id=str(run_id),
        trigger=trigger,
        as_of_session=str(as_of_session),
    )

    # ── Phase A: assemble bounded per-engine payload ──────────────────
    payload = await assemble_aar_payload(pool, as_of_session=as_of_session)
    engines_examined = tuple(w.engine for w in payload)

    # ── Phase B: LLM-driven finding emission ─────────────────────────
    findings: tuple[AARFinding, ...] = ()
    rejection_reason: str | None = None

    if llm_callable is not None:
        try:
            findings = await _drive_llm_call(
                llm_callable=llm_callable,
                payload=payload,
                as_of_session=as_of_session,
            )
        except CriticError as exc:
            rejection_reason = str(exc)
            log.warning("aar_critic.run.critic_error", error=str(exc))
    else:
        rejection_reason = "smoke_mode_no_llm"

    # ── Phase C: persist provenance + per-finding rows ───────────────
    completed_ts = datetime.now(UTC)
    findings_emitted = tuple(f.finding_id for f in findings)
    run = AARCriticRun(
        run_id=run_id,
        started_ts=started_ts,
        completed_ts=completed_ts,
        trigger=trigger,
        as_of_session=as_of_session,
        engines_examined=engines_examined,
        findings_emitted=findings_emitted,
        persona_version=PERSONA_VERSION,
        rejection_reason=rejection_reason,
    )
    await record_aar_critic_run(pool, run)
    for finding in findings:
        await record_aar_finding(pool, run_id=str(run_id), finding=finding)

    # ── Phase D: memstore archival (best-effort, application_log binds) ──
    if findings and anthropic_client is not None:
        if aar_memstore_id is not None:
            for finding in findings:
                await archive_finding_to_aar_memstore(
                    finding,
                    memstore_id=aar_memstore_id,
                    client=anthropic_client,
                )
        if finder_memstore_id is not None:
            for finding in findings:
                await copy_finding_to_finder_memstore(
                    finding,
                    finder_memstore_id=finder_memstore_id,
                    client=anthropic_client,
                )

    log.info(
        "aar_critic.run.complete",
        run_id=str(run_id),
        engines=len(engines_examined),
        findings=len(findings),
        rejection_reason=rejection_reason,
    )
    return run


# ───────────────────────── LLM call driver (single-turn) ─────────────────


async def _drive_llm_call(
    *,
    llm_callable: LLMCallable,
    payload: tuple[EnginePerformanceWindow, ...],
    as_of_session: date_t,
) -> tuple[AARFinding, ...]:
    """Run one LLM turn → decode → validate findings.

    The critic is SINGLE-TURN by design (no tool dispatch loop like the
    finder). The LLM sees the payload + persona + memstore on Anthropic's
    side; emits one JSON envelope; we validate + return.
    """
    system_prompt = persona_text()
    user_prompt = _compose_user_prompt(payload, as_of_session)

    envelope = await llm_callable(system_prompt, user_prompt, [])
    decoded = envelope if isinstance(envelope, dict) else {}

    if decoded.get("kind") != "AARCriticResponse":
        # Unexpected envelope shape — surface but do not crash.
        log.warning(
            "aar_critic.envelope.unexpected_kind",
            kind=decoded.get("kind"),
        )
        return ()

    raw_findings = decoded.get("findings", [])
    if not isinstance(raw_findings, list):
        log.warning("aar_critic.envelope.findings_not_list")
        return ()

    validated: list[AARFinding] = []
    rejection_notes: list[str] = []
    per_engine_count: dict[str, int] = {}
    for i, raw in enumerate(raw_findings):
        if not isinstance(raw, dict):
            rejection_notes.append(f"finding[{i}]: not a dict")
            continue
        normalized = _normalize_finding(raw, as_of_session=as_of_session)
        engine = normalized.get("engine", "?")
        # Cap per-engine emissions per run (fence — spec §6 + persona §6).
        if per_engine_count.get(str(engine), 0) >= MAX_FINDINGS_PER_ENGINE_PER_RUN:
            rejection_notes.append(
                f"finding[{i}]: per-engine cap reached for {engine}"
            )
            continue
        try:
            finding = AARFinding(**normalized)
            validated.append(finding)
            per_engine_count[str(engine)] = per_engine_count.get(str(engine), 0) + 1
        except ValidationError as exc:
            rejection_notes.append(f"finding[{i}]: {str(exc)[:200]}")

    if rejection_notes:
        log.warning(
            "aar_critic.findings.partial_rejection",
            count_validated=len(validated),
            count_rejected=len(rejection_notes),
            notes=rejection_notes[:5],
        )

    return tuple(validated)


def _normalize_finding(
    raw: dict[str, Any], *, as_of_session: date_t
) -> dict[str, Any]:
    """Stamp persona_version + compute finding_id from (engine, theme, session).

    The LLM does NOT supply finding_id or persona_version — the application
    fills them deterministically per spec §3.1 + §7 (persona).

    Also tolerates the LLM emitting observation_session as a string —
    pydantic will coerce, but compute_finding_id needs a date.
    """
    out = dict(raw)
    out["persona_version"] = PERSONA_VERSION
    # observation_session: default to as_of_session if missing/invalid.
    obs = out.get("observation_session", as_of_session)
    if isinstance(obs, str):
        try:
            obs_date = date_t.fromisoformat(obs)
        except (ValueError, TypeError):
            obs_date = as_of_session
    elif isinstance(obs, date_t):
        obs_date = obs
    else:
        obs_date = as_of_session
    out["observation_session"] = obs_date
    # Compute deterministic finding_id from (engine, theme, session).
    engine = str(out.get("engine", ""))
    theme = str(out.get("theme", ""))
    out["finding_id"] = compute_finding_id(engine, theme, obs_date)
    return out


def _compose_user_prompt(
    payload: tuple[EnginePerformanceWindow, ...],
    as_of_session: date_t,
) -> str:
    """Build the user prompt — payload summary + emission directive.

    The persona (system prompt) already explains the substrate. The user
    prompt is the data + the explicit "now emit" cue.
    """
    if not payload:
        return (
            f"## AAR payload (session_date={as_of_session.isoformat()})\n\n"
            "No engines have AAR events in scope.\n\n"
            "Respond with {'kind': 'AARCriticResponse', 'findings': [], "
            "'rationale': 'no engines in payload'}."
        )

    sections: list[str] = []
    for w in payload:
        sections.append(_render_engine_section(w))

    body = "\n\n".join(sections)
    return (
        f"## AAR payload (session_date={as_of_session.isoformat()})\n\n"
        f"You are reviewing {len(payload)} engine(s). Read the persona §3 + §4 "
        f"+ §5 before emitting. Emit ONLY findings supported by the numbers "
        f"shown below. Empty findings: [] is valid if nothing surfaces.\n\n"
        f"### Per-engine payload\n\n{body}\n\n"
        "### Output\n\n"
        "Respond ONLY with a JSON envelope:\n"
        "{\n"
        "  'kind': 'AARCriticResponse',\n"
        "  'findings': [{'engine': ..., 'theme': ..., 'pattern_observed': ..., 'suggested_emission_axis': ..., 'evidence_aar_count': N, 'evidence_window_sessions': N, 'confidence': 'low'|'medium'|'high', 'observation_session': '"
        f"{as_of_session.isoformat()}"
        "'}],\n"
        "  'rationale': '<200 char summary>'\n"
        "}\n\n"
        "Themes (closed Literal — pick ONE):\n"
        "exit_timing | entry_quality | sizing_drift | regime_conditional_perf | "
        "exit_reason_skew | rule_compliance_drift | hold_duration_skew | "
        "slippage_drift | win_rate_decay\n\n"
        "Cap: max 5 findings per engine per run. Quality > volume."
    )


def _render_engine_section(w: EnginePerformanceWindow) -> str:
    """Render one EnginePerformanceWindow into compact prose for the LLM."""
    lines: list[str] = [
        f"#### {w.engine}",
        f"- trade_count_total: {w.trade_count_total}",
        f"- trade_count_window: {w.trade_count_window}",
        f"- pnl_net_total_usd: {w.pnl_net_total_usd}",
        f"- pnl_net_window_usd: {w.pnl_net_window_usd}",
        f"- win_rate_window: {w.win_rate_window:.3f}",
        f"- win_rate_total: {w.win_rate_total:.3f}",
        f"- rule_compliance_rate: {w.rule_compliance_rate:.3f}",
    ]
    if w.slippage_bps_p50 is not None:
        lines.append(f"- slippage_bps_p50: {w.slippage_bps_p50:.2f}")
    if w.slippage_bps_p95 is not None:
        lines.append(f"- slippage_bps_p95: {w.slippage_bps_p95:.2f}")

    lines.append("- exit_reason_distribution:")
    for reason in sorted(w.exit_reason_distribution):
        cnt = w.exit_reason_distribution[reason]
        pnl = w.exit_reason_pnl_by_reason_usd.get(reason, "?")
        lines.append(f"    - {reason}: {cnt} trades, pnl={pnl}")

    lines.append("- hold_duration_buckets:")
    for bucket in ("0-1d", "1-3d", "3-7d", "7-21d", "21d+"):
        cnt = w.hold_duration_buckets.get(bucket, 0)  # type: ignore[arg-type]
        pnl = w.pnl_per_hold_bucket_usd.get(bucket, "?")  # type: ignore[arg-type]
        lines.append(f"    - {bucket}: {cnt} trades, pnl={pnl}")

    if w.recent_aars:
        lines.append(f"- recent_aars: {len(w.recent_aars)} entries (last 20 by exit)")
        # Show first 5 for brevity in the prompt
        for r in w.recent_aars[:5]:
            lines.append(
                f"    - {r.ticker} exit={r.exit_session.isoformat()} "
                f"pnl={r.pnl_net_usd} reason={r.exit_reason} hold={r.hold_sessions}"
            )

    return "\n".join(lines)


# ───────────────────────── CLI (operator-discretion invocation) ──────────


def main() -> int:
    """Operator CLI: ``python -m ops.llm_aar_critic --since <YYYY-MM-DD>``.

    Optional flags:
        ``--engine <name>`` — restrict the engines_examined list to a single
            engine (the assembler still pulls the full table; downstream
            filtering happens at the LLM prompt + finding emission).
        ``--smoke`` — assembly + provenance only; no LLM call, no memstore.
        ``--no-memstore`` — skip memstore archival even with LLM enabled.
    """
    import argparse
    import asyncio
    import os

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help="ISO session date to anchor the 90-session window (default = today UTC).",
    )
    parser.add_argument(
        "--engine",
        type=str,
        default=None,
        help="Restrict examined engines to this single name (e.g. 'catalyst').",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Run without LLM (assembly + provenance only).",
    )
    parser.add_argument(
        "--no-memstore",
        action="store_true",
        help="Skip memstore archival (application_log row remains).",
    )
    args = parser.parse_args()

    if args.since:
        as_of = date_t.fromisoformat(args.since)
    else:
        as_of = datetime.now(UTC).date()

    db_url = os.environ.get("DATABASE_URL")
    if db_url is None:
        print("DATABASE_URL not set — refusing to proceed.", flush=True)
        return 1

    async def _run() -> AARCriticRun:
        import asyncpg
        pool = await asyncpg.create_pool(db_url, min_size=1, max_size=2)
        try:
            llm_callable: LLMCallable | None = None
            anthropic_client = None
            aar_memstore_id = None
            finder_memstore_id = None
            if not args.smoke:
                from anthropic import AsyncAnthropic

                from ops.llm_aar_anthropic_ids import (
                    AAR_CRITIC_MEMSTORE_ID,
                    FINDER_MEMSTORE_ID,
                )
                from ops.llm_aar_critic_sdk import make_sdk_aar_callable
                anthropic_client = AsyncAnthropic()
                llm_callable = make_sdk_aar_callable(client=anthropic_client)
                if not args.no_memstore:
                    aar_memstore_id = AAR_CRITIC_MEMSTORE_ID
                    finder_memstore_id = FINDER_MEMSTORE_ID
            return await run_aar_critic(
                pool,
                trigger="operator_command",
                as_of_session=as_of,
                llm_callable=llm_callable,
                anthropic_client=anthropic_client,
                aar_memstore_id=aar_memstore_id,
                finder_memstore_id=finder_memstore_id,
            )
        finally:
            await pool.close()

    run = asyncio.run(_run())
    print(f"run_id: {run.run_id}")
    print(f"engines_examined: {len(run.engines_examined)} {list(run.engines_examined)}")
    print(f"findings_emitted: {len(run.findings_emitted)} {list(run.findings_emitted)}")
    if run.rejection_reason:
        print(f"rejection_reason: {run.rejection_reason}")
    if args.engine and args.engine not in run.engines_examined:
        print(
            f"warning: --engine {args.engine!r} requested but not found in "
            f"engines_examined; the LLM saw the full payload."
        )
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())


__all__ = ["CriticError", "LLMCallable", "main", "run_aar_critic"]
