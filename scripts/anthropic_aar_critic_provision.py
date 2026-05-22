"""Idempotent one-time provisioner — Anthropic Agent + Environment + Memstore for the LLM-AAR critic.

Spec ``docs/superpowers/specs/2026-05-22-llm-aar-critic-design.md`` §11 T5.

Creates:
  1. ``Memstore`` (named ``aar-llm-critic-context``) — dedicated AAR critic
     memstore per spec §4.1. Stores /findings/<engine>/<finding_id>.md,
     /recent-runs/<run_id>.md, /agent-context/curation-policy.md,
     /lessons/<operator>.md.
  2. ``Agent`` (named ``lab-aar-critic``) with the persona at v1.0+ as its
     ``system`` field, ``claude-sonnet-4-6`` as the model, and the
     ``agent_toolset_20260401`` toolset enabled with ``read``, ``write``,
     ``edit``, ``glob``, ``grep`` available (so the agent can read/write
     the mounted memstore directory). ``bash``, ``web_fetch``, and
     ``web_search`` are DISABLED (advisory-only fence per persona §9).
  3. ``Environment`` (named ``lab-aar-critic-env``) with no extra
     packages — the agent never executes platform code, only reads/writes
     memstore files + emits JSON envelopes for the application to parse.

All three are looked up by name first; only created if missing. On
success the IDs are persisted to ``ops/llm_aar_anthropic_ids.py``.

Usage::

    python -m scripts.anthropic_aar_critic_provision           # provision (or no-op if present)
    python -m scripts.anthropic_aar_critic_provision --rebuild # archive + recreate

Operator-binding: the persona text written to the agent's ``system`` field
is read DIRECTLY from ``docs/llm_aar_persona.md`` (single source of
truth). Drift between local persona file + agent.system is impossible
because the provisioner does NOT keep an inlined copy.

Mirrors ``scripts/anthropic_agent_provision.py`` (PR #294) shape; the
delta is that this provisioner also creates a dedicated memstore (the
finder provisioner reused an operator-pre-seeded memstore).
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import sys
from pathlib import Path

import structlog
from anthropic import AsyncAnthropic

# Constants module — we write the IDs into this file.
IDS_MODULE_PATH = (
    Path(__file__).resolve().parent.parent / "ops" / "llm_aar_anthropic_ids.py"
)
PERSONA_PATH = (
    Path(__file__).resolve().parent.parent / "docs" / "llm_aar_persona.md"
)

MEMSTORE_NAME = "aar-llm-critic-context"
MEMSTORE_DESCRIPTION = (
    "Post-trade pattern recognition for the autonomous engine-improvement loop. "
    "Reads platform.aar_events; writes AARFinding records into "
    "/findings/<engine>/<finding_id>.md. Cross-read by the finder memstore."
)

AGENT_NAME = "lab-aar-critic"
ENVIRONMENT_NAME = "lab-aar-critic-env"
MODEL = "claude-sonnet-4-6"
MANAGED_AGENTS_BETA = "managed-agents-2026-04-01"

# The finder memstore the AAR critic cross-writes findings into (per spec §4.3).
# Operator-seeded 2026-05-22 — stable; do not regenerate.
FINDER_MEMSTORE_ID = "memstore_01MzLun3AfRf2viPmDqJvsWi"

log = structlog.get_logger(__name__)


def _read_persona() -> tuple[str, str]:
    """Return (persona_text, sha256_hex). Pinned in the IDs module."""
    text = PERSONA_PATH.read_text(encoding="utf-8")
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return text, sha


async def _find_existing_memstore(
    client: AsyncAnthropic, name: str
) -> str | None:
    """Return the memory_store_id for an existing memstore of the given name, or None."""
    page = await client.beta.memory_stores.list(betas=[MANAGED_AGENTS_BETA])
    for ms in page.data:
        if ms.name == name and getattr(ms, "archived_at", None) is None:
            return ms.id
    while getattr(page, "has_more", False):
        page = await client.beta.memory_stores.list(
            betas=[MANAGED_AGENTS_BETA], after_id=page.last_id  # type: ignore[arg-type]
        )
        for ms in page.data:
            if ms.name == name and getattr(ms, "archived_at", None) is None:
                return ms.id
    return None


async def _create_memstore(client: AsyncAnthropic) -> str:
    """Create the aar-llm-critic-context memstore. Returns memory_store_id."""
    ms = await client.beta.memory_stores.create(
        name=MEMSTORE_NAME,
        description=MEMSTORE_DESCRIPTION,
        betas=[MANAGED_AGENTS_BETA],
    )
    return ms.id


async def _seed_curation_policy(
    client: AsyncAnthropic, memstore_id: str
) -> None:
    """Seed /agent-context/curation-policy.md per spec §4.4."""
    policy = (
        "# AAR-Critic Memstore Curation Policy\n\n"
        "**Authority:** operator-staged. The LLM reads this but never writes "
        "to /agent-context/.\n\n"
        "## Caps\n\n"
        "- /findings/<engine>/ — max 30 files per engine (application-managed LRU).\n"
        "- /recent-runs/ — max 20 files (application-managed LRU).\n"
        "- /lessons/ — operator-managed; agent reads, never writes.\n\n"
        "## Write discipline\n\n"
        "- Every finding MUST have evidence_aar_count >= 3 (pydantic-enforced).\n"
        "- Confidence band mechanically cross-validated against evidence_aar_count.\n"
        "- No LLM-managed deletion. Application-managed LRU only.\n\n"
        "## Findings cross-memstore copy\n\n"
        "Every finding written to /findings/<engine>/<finding_id>.md is "
        "ALSO curated-copied by the application to the finder memstore at "
        "/aar-findings/<engine>/<finding_id>.md. The agent does not touch the "
        "finder memstore directly.\n\n"
        "## Persona version\n\n"
        "Persona is SHA-pinned in tpcore.lab.llm_aar.PERSONA_SHA256. Drift "
        "trips CI sentinel. Persona edits MUST re-run "
        "scripts/anthropic_aar_critic_provision.py --rebuild.\n"
    )
    try:
        await client.beta.memory_stores.memories.create(
            memory_store_id=memstore_id,
            content=policy,
            path="/agent-context/curation-policy.md",
            betas=[MANAGED_AGENTS_BETA],
        )
        log.info("anthropic_provision.curation_policy_seeded", memstore_id=memstore_id)
    except Exception as exc:  # noqa: BLE001 — best-effort
        log.warning(
            "anthropic_provision.curation_policy_seed_failed",
            error=str(exc)[:200],
        )


async def _find_existing_agent(client: AsyncAnthropic, name: str) -> str | None:
    """Return the agent_id for an existing agent of the given name, or None."""
    page = await client.beta.agents.list(betas=[MANAGED_AGENTS_BETA])
    for agent in page.data:
        if agent.name == name and getattr(agent, "archived_at", None) is None:
            return agent.id
    while getattr(page, "has_more", False):
        page = await client.beta.agents.list(
            betas=[MANAGED_AGENTS_BETA], after_id=page.last_id  # type: ignore[arg-type]
        )
        for agent in page.data:
            if agent.name == name and getattr(agent, "archived_at", None) is None:
                return agent.id
    return None


async def _find_existing_environment(client: AsyncAnthropic, name: str) -> str | None:
    """Return the environment_id for an existing env of the given name, or None."""
    page = await client.beta.environments.list(betas=[MANAGED_AGENTS_BETA])
    for env in page.data:
        if env.name == name and getattr(env, "archived_at", None) is None:
            return env.id
    while getattr(page, "has_more", False):
        page = await client.beta.environments.list(
            betas=[MANAGED_AGENTS_BETA], after_id=page.last_id  # type: ignore[arg-type]
        )
        for env in page.data:
            if env.name == name and getattr(env, "archived_at", None) is None:
                return env.id
    return None


async def _create_environment(client: AsyncAnthropic) -> str:
    """Create the lab-aar-critic environment. Returns env_id."""
    env = await client.beta.environments.create(
        name=ENVIRONMENT_NAME,
        description="LLM-AAR critic runtime — memstore-only, no bash, no web.",
        betas=[MANAGED_AGENTS_BETA],
    )
    return env.id


async def _create_agent(client: AsyncAnthropic, persona_text: str) -> str:
    """Create the lab-aar-critic agent with persona pinned. Returns agent_id."""
    agent = await client.beta.agents.create(
        model=MODEL,
        name=AGENT_NAME,
        description=(
            "Autonomous LLM-AAR critic for the short-term-trading-engine "
            "platform. Reads per-engine AAR aggregates + identifies "
            "behavioural patterns; emits AARFinding records the finder reads "
            "as hypothesis seeds. Advisory-only — never mutates engines, "
            "never opens PRs. Spec 2026-05-22-llm-aar-critic-design.md."
        ),
        system=persona_text,
        tools=[
            {
                "type": "agent_toolset_20260401",
                "default_config": {
                    "enabled": False,  # default OFF; per-tool overrides below
                    "permission_policy": {"type": "always_allow"},
                },
                "configs": [
                    # Memstore directory access — needed for /agent-context/,
                    # /findings/, /recent-runs/, /lessons/.
                    {"name": "read", "enabled": True, "permission_policy": {"type": "always_allow"}},
                    {"name": "write", "enabled": True, "permission_policy": {"type": "always_allow"}},
                    {"name": "edit", "enabled": True, "permission_policy": {"type": "always_allow"}},
                    {"name": "glob", "enabled": True, "permission_policy": {"type": "always_allow"}},
                    {"name": "grep", "enabled": True, "permission_policy": {"type": "always_allow"}},
                    # Off-reservation tools — persona §9 fence + safety
                    # invariant. The agent cannot execute platform code OR
                    # exfiltrate via web requests.
                    {"name": "bash", "enabled": False},
                    {"name": "web_fetch", "enabled": False},
                    {"name": "web_search", "enabled": False},
                ],
            }
        ],
        betas=[MANAGED_AGENTS_BETA],
    )
    return agent.id


def _write_ids_module(
    agent_id: str,
    environment_id: str,
    memstore_id: str,
    persona_sha: str,
) -> None:
    """Persist the IDs to ops/llm_aar_anthropic_ids.py (idempotent overwrite)."""
    body = (
        '"""Anthropic Managed-Agents IDs for the LLM-AAR critic — generated by\n'
        "``scripts/anthropic_aar_critic_provision.py``.\n\n"
        "Constants module. The critic SDK reads ``AAR_CRITIC_AGENT_ID`` +\n"
        "``AAR_CRITIC_ENVIRONMENT_ID`` + ``AAR_CRITIC_MEMSTORE_ID`` to attach\n"
        "sessions to the right Managed Agent + environment + dedicated AAR\n"
        "memstore.\n\n"
        "The finder memstore ID is ALSO carried here so the AAR critic can copy\n"
        "findings into the finder's memstore at /aar-findings/<engine>/ (per\n"
        "spec §4.3 + §7).\n\n"
        "DO NOT hand-edit. Regenerate via:\n"
        "    python -m scripts.anthropic_aar_critic_provision\n"
        '"""\n'
        "from __future__ import annotations\n\n"
        f'AAR_CRITIC_AGENT_ID: str = "{agent_id}"\n'
        '"""Set by scripts/anthropic_aar_critic_provision.py."""\n\n'
        f'AAR_CRITIC_ENVIRONMENT_ID: str = "{environment_id}"\n'
        '"""Set by scripts/anthropic_aar_critic_provision.py."""\n\n'
        f'AAR_CRITIC_MEMSTORE_ID: str = "{memstore_id}"\n'
        "\"\"\"Created by scripts/anthropic_aar_critic_provision.py with\n"
        "name='aar-llm-critic-context'. Per spec §4.1.\"\"\"\n\n"
        f'FINDER_MEMSTORE_ID: str = "{FINDER_MEMSTORE_ID}"\n'
        '"""The Task #25 finder memstore — operator-seeded 2026-05-22 handoff.\n'
        "AAR findings are curated-copied here at /aar-findings/<engine>/<finding_id>.md\n"
        'per spec §4.3 + §7. Stable; do not regenerate."""\n\n'
        f'PROVISIONED_PERSONA_SHA256: str = "{persona_sha}"\n'
        '"""SHA256 of the persona text written to the agent\'s ``system`` field at\n'
        'provision time. The critic SDK warns on drift (defense-in-depth)."""\n\n'
        'MANAGED_AGENTS_BETA: str = "managed-agents-2026-04-01"\n'
        '"""Required beta header on every Sessions API call. Per Anthropic\n'
        'managed-agents API current docs (2026-05-22 via context7 MCP)."""\n\n\n'
        "__all__ = [\n"
        '    "AAR_CRITIC_AGENT_ID",\n'
        '    "AAR_CRITIC_ENVIRONMENT_ID",\n'
        '    "AAR_CRITIC_MEMSTORE_ID",\n'
        '    "FINDER_MEMSTORE_ID",\n'
        '    "MANAGED_AGENTS_BETA",\n'
        '    "PROVISIONED_PERSONA_SHA256",\n'
        "]\n"
    )
    IDS_MODULE_PATH.write_text(body, encoding="utf-8")


async def _provision(rebuild: bool = False) -> tuple[str, str, str, str]:
    """Provision (or look up) the agent + environment + memstore.

    Returns (agent_id, environment_id, memstore_id, persona_sha).
    """
    if "ANTHROPIC_API_KEY" not in os.environ:
        raise SystemExit("ANTHROPIC_API_KEY not set — refusing to proceed.")
    persona_text, persona_sha = _read_persona()
    log.info(
        "anthropic_provision.persona_loaded",
        sha256=persona_sha,
        bytes=len(persona_text.encode("utf-8")),
    )
    async with AsyncAnthropic() as client:
        ms_id = None if rebuild else await _find_existing_memstore(client, MEMSTORE_NAME)
        if ms_id is None:
            ms_id = await _create_memstore(client)
            log.info("anthropic_provision.memstore_created", memstore_id=ms_id)
            await _seed_curation_policy(client, ms_id)
        else:
            log.info("anthropic_provision.memstore_reused", memstore_id=ms_id)

        env_id = None if rebuild else await _find_existing_environment(client, ENVIRONMENT_NAME)
        if env_id is None:
            env_id = await _create_environment(client)
            log.info("anthropic_provision.environment_created", environment_id=env_id)
        else:
            log.info("anthropic_provision.environment_reused", environment_id=env_id)

        agent_id = None if rebuild else await _find_existing_agent(client, AGENT_NAME)
        if agent_id is None:
            agent_id = await _create_agent(client, persona_text)
            log.info("anthropic_provision.agent_created", agent_id=agent_id)
        else:
            log.info("anthropic_provision.agent_reused", agent_id=agent_id)
    return agent_id, env_id, ms_id, persona_sha


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Always create new agent + environment + memstore (skip lookup). Use after persona edits.",
    )
    args = parser.parse_args(argv)

    agent_id, env_id, ms_id, persona_sha = asyncio.run(
        _provision(rebuild=args.rebuild)
    )
    _write_ids_module(agent_id, env_id, ms_id, persona_sha)
    print(f"agent_id:       {agent_id}")
    print(f"environment_id: {env_id}")
    print(f"memstore_id:    {ms_id}")
    print(f"persona_sha256: {persona_sha}")
    print(f"wrote:          {IDS_MODULE_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
