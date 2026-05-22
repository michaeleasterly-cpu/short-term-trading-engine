"""Memstore writers for AARFinding archival — spec §4.

The critic's findings land in TWO places:
1. ``aar-llm-critic-context`` memstore at ``/findings/<engine>/<finding_id>.md``
   — the agent's own cross-run memory.
2. The finder's memstore at ``/aar-findings/<engine>/<finding_id>.md`` —
   curated copy so the finder reads AAR findings at startup.

Both writes are best-effort: the application_log row (via
``record_aar_finding``) is the authoritative emission record. Memstore
writes are cross-agent cache; failures log a warning but never raise.

Reads CURRENT Anthropic Managed Agents API per
https://platform.claude.com/docs/en/api/cli/beta/memory_stores/memories
(POST /v1/memory_stores/{memory_store_id}/memories).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from tpcore.lab.llm_aar.models import AARFinding

if TYPE_CHECKING:  # pragma: no cover
    from anthropic import AsyncAnthropic

log = structlog.get_logger(__name__)


# Beta header required for the Managed Agents memstore API. Mirrors
# PR #294's MANAGED_AGENTS_BETA constant.
MANAGED_AGENTS_BETA: str = "managed-agents-2026-04-01"


def render_finding_markdown(finding: AARFinding) -> str:
    """Render an AARFinding to operator-readable markdown.

    Used for BOTH memstore writes (critic's + finder's curated copy) +
    operator inspection. Same content; the path differs.
    """
    return (
        f"# {finding.finding_id}\n\n"
        f"**Engine:** {finding.engine}\n"
        f"**Theme:** {finding.theme}\n"
        f"**Confidence:** {finding.confidence}\n"
        f"**Evidence:** {finding.evidence_aar_count} AARs over "
        f"{finding.evidence_window_sessions} sessions\n"
        f"**Observation session:** {finding.observation_session.isoformat()}\n"
        f"**Persona version:** {finding.persona_version}\n\n"
        f"## Pattern observed\n\n{finding.pattern_observed}\n\n"
        f"## Suggested emission axis\n\n{finding.suggested_emission_axis}\n"
    )


async def archive_finding_to_aar_memstore(
    finding: AARFinding,
    *,
    memstore_id: str,
    client: AsyncAnthropic | None = None,
) -> str | None:
    """Write a finding to the AAR critic's own memstore.

    Path: ``/findings/<engine>/<finding_id>.md``. The Anthropic API
    creates the memory; the deterministic finding_id means re-emitting
    the same pattern on the same observation_session is idempotent at
    the path (same path -> overwrite).

    Returns the memory_id (``mem_...``) on success; ``None`` on failure
    (logged warning, NEVER raises — application_log is the binding
    record, this is best-effort).
    """
    return await _create_memory(
        client=client,
        memstore_id=memstore_id,
        path=f"/findings/{finding.engine}/{finding.finding_id}.md",
        content=render_finding_markdown(finding),
        log_label="aar_critic.finding_archived",
        log_context={
            "finding_id": finding.finding_id,
            "engine": finding.engine,
        },
    )


async def copy_finding_to_finder_memstore(
    finding: AARFinding,
    *,
    finder_memstore_id: str,
    client: AsyncAnthropic | None = None,
) -> str | None:
    """Curated copy of a finding into the finder memstore — spec §4.3.

    Path: ``/aar-findings/<engine>/<finding_id>.md`` in the finder's
    memstore. When the finder targets ``engine=X``, its persona §11
    amendment (future PR) directs it to read ``/aar-findings/X/``.

    This is application-side machinery; the AAR critic does not touch
    the finder memstore directly. Cross-memstore curated copy keeps the
    finder's prompt-tokens bounded vs. multi-memstore attach.
    """
    return await _create_memory(
        client=client,
        memstore_id=finder_memstore_id,
        path=f"/aar-findings/{finding.engine}/{finding.finding_id}.md",
        content=render_finding_markdown(finding),
        log_label="aar_critic.finding_copied_to_finder",
        log_context={
            "finding_id": finding.finding_id,
            "engine": finding.engine,
            "finder_memstore_id": finder_memstore_id,
        },
    )


async def write_run_summary_to_memstore(
    *,
    memstore_id: str,
    run_id: str,
    summary_markdown: str,
    client: AsyncAnthropic | None = None,
) -> str | None:
    """Write a per-run summary file to ``/recent-runs/<run_id>.md``.

    Application-side; the agent reads /recent-runs/ at startup per
    persona §8 to diff-from-last-run.
    """
    return await _create_memory(
        client=client,
        memstore_id=memstore_id,
        path=f"/recent-runs/{run_id}.md",
        content=summary_markdown,
        log_label="aar_critic.run_summary_archived",
        log_context={"run_id": run_id},
    )


async def _create_memory(
    *,
    client: AsyncAnthropic | None,
    memstore_id: str,
    path: str,
    content: str,
    log_label: str,
    log_context: dict[str, str],
) -> str | None:
    """Shared best-effort memstore.memories.create wrapper.

    Centralises the API call shape so all three writers agree on the
    beta header + error handling.
    """
    if client is None:
        # Lazy import — anthropic is a runtime dep but tests inject a fake
        # client so we never instantiate the SDK in unit tests.
        from anthropic import AsyncAnthropic as _A
        client = _A()
    try:
        memory = await client.beta.memory_stores.memories.create(
            memory_store_id=memstore_id,
            content=content,
            path=path,
            betas=[MANAGED_AGENTS_BETA],
        )
        log.info(log_label, path=path, memory_id=memory.id, **log_context)
        return memory.id
    except Exception as exc:  # noqa: BLE001 — best-effort; app_log is the record
        log.warning(
            f"{log_label}_failed",
            path=path,
            error=str(exc)[:200],
            **log_context,
        )
        return None


__all__ = [
    "MANAGED_AGENTS_BETA",
    "archive_finding_to_aar_memstore",
    "copy_finding_to_finder_memstore",
    "render_finding_markdown",
    "write_run_summary_to_memstore",
]
