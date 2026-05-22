"""Idempotent one-time provisioner — Anthropic Agent + Environment for the LLM edge-finder.

Task #25 + 2026-05-22 Sessions API wiring.

Creates:
  1. ``Agent`` (named ``lab-edge-finder``) with the persona at v2.3+ as its
     ``system`` field, ``claude-sonnet-4-6`` as the model, and the
     ``agent_toolset_20260401`` toolset enabled with ``read``, ``write``,
     ``edit``, ``glob``, ``grep`` available (so the agent can read/write
     the mounted memstore directory). ``bash``, ``web_fetch``, and
     ``web_search`` are DISABLED (off-reservation per persona §2.8 fence).
  2. ``Environment`` (named ``lab-edge-finder-env``) with no extra
     packages — the agent never executes platform code, it just reads/writes
     memstore files + emits JSON envelopes for the application to parse.

Both are looked up by name first; only created if missing. On success the
IDs are persisted to ``ops/llm_finder_anthropic_ids.py``.

Usage::

    python -m scripts.anthropic_agent_provision           # provision (or no-op if present)
    python -m scripts.anthropic_agent_provision --rebuild # archive + recreate

Operator-binding: the persona text written to the agent's ``system`` field
is read DIRECTLY from ``docs/lab_finder_persona.md`` (single source of
truth — same path the local ``persona.py`` reads). Drift between local
persona file + agent.system is impossible because the provisioner does NOT
keep an inlined copy.
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
IDS_MODULE_PATH = Path(__file__).resolve().parent.parent / "ops" / "llm_finder_anthropic_ids.py"
PERSONA_PATH = Path(__file__).resolve().parent.parent / "docs" / "lab_finder_persona.md"

AGENT_NAME = "lab-edge-finder"
ENVIRONMENT_NAME = "lab-edge-finder-env"
MODEL = "claude-sonnet-4-6"
MANAGED_AGENTS_BETA = "managed-agents-2026-04-01"

log = structlog.get_logger(__name__)


def _read_persona() -> tuple[str, str]:
    """Return (persona_text, sha256_hex). Pinned in the IDs module."""
    text = PERSONA_PATH.read_text(encoding="utf-8")
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return text, sha


async def _find_existing_agent(client: AsyncAnthropic, name: str) -> str | None:
    """Return the agent_id for an existing agent of the given name, or None."""
    page = await client.beta.agents.list(betas=[MANAGED_AGENTS_BETA])
    for agent in page.data:
        if agent.name == name and getattr(agent, "archived_at", None) is None:
            return agent.id
    # Paginate if there's more (defensive — current quotas are tiny but be correct)
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
    """Create the lab-edge-finder environment. Returns env_id."""
    env = await client.beta.environments.create(
        name=ENVIRONMENT_NAME,
        description="LLM edge-finder runtime — memstore-only, no bash, no web.",
        betas=[MANAGED_AGENTS_BETA],
    )
    return env.id


async def _create_agent(client: AsyncAnthropic, persona_text: str) -> str:
    """Create the lab-edge-finder agent with persona pinned. Returns agent_id."""
    agent = await client.beta.agents.create(
        model=MODEL,
        name=AGENT_NAME,
        description=(
            "Autonomous LLM edge-finder for the short-term-trading-engine "
            "platform. Reads regime snapshot + prior emissions from "
            "memstore, emits regime-conditional ProposedSpecs via JSON "
            "envelopes that the application parses + routes through the "
            "SP-A statistical gate. Task #25."
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
                    # /prior-emissions/, /outcomes/, /lessons/, /sessions/.
                    {"name": "read", "enabled": True, "permission_policy": {"type": "always_allow"}},
                    {"name": "write", "enabled": True, "permission_policy": {"type": "always_allow"}},
                    {"name": "edit", "enabled": True, "permission_policy": {"type": "always_allow"}},
                    {"name": "glob", "enabled": True, "permission_policy": {"type": "always_allow"}},
                    {"name": "grep", "enabled": True, "permission_policy": {"type": "always_allow"}},
                    # Off-reservation tools — persona §2.8 fence + safety
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


def _write_ids_module(agent_id: str, environment_id: str, persona_sha: str) -> None:
    """Persist the IDs to ops/llm_finder_anthropic_ids.py (idempotent overwrite)."""
    body = (
        '"""Anthropic Managed-Agents IDs — generated by '
        "``scripts/anthropic_agent_provision.py``.\n\n"
        "Constants module. The finder SDK reads ``EDGE_FINDER_AGENT_ID`` +\n"
        "``EDGE_FINDER_ENVIRONMENT_ID`` to attach sessions to the right\n"
        "managed agent + environment. The ``EDGE_FINDER_MEMSTORE_ID`` is\n"
        "the operator's pre-seeded finder memstore from the 2026-05-22\n"
        "Anthropic memory-store handoff.\n\n"
        "DO NOT hand-edit. Regenerate via ``python -m\n"
        "scripts.anthropic_agent_provision``.\n"
        '"""\n'
        "from __future__ import annotations\n\n"
        f'EDGE_FINDER_AGENT_ID: str = "{agent_id}"\n'
        f'EDGE_FINDER_ENVIRONMENT_ID: str = "{environment_id}"\n'
        'EDGE_FINDER_MEMSTORE_ID: str = "memstore_01MzLun3AfRf2viPmDqJvsWi"\n'
        '"""Operator-seeded finder memstore — 2026-05-22 handoff.\n\n'
        "Contains: /agent-context/, /cross-agent/dev-to-finder/,\n"
        "/outcomes/, /prior-emissions/ (6 emissions from v2.0 gate pilot).\n"
        "The finder reads these on startup + writes /sessions/<run_id>.md\n"
        'at completion per persona §11."""\n\n'
        f'PROVISIONED_PERSONA_SHA256: str = "{persona_sha}"\n'
        '"""SHA256 of the persona text written to the agent\'s ``system``\n'
        "field at provision time. The finder SDK asserts this matches\n"
        "``tpcore.lab.llm_finder.PERSONA_SHA256`` at runtime — mismatch\n"
        'means the persona file was edited without re-provisioning."""\n\n'
        "MANAGED_AGENTS_BETA: str = \"managed-agents-2026-04-01\"\n"
        '"""Required beta header on every Sessions API call."""\n\n'
        "__all__ = [\n"
        '    "EDGE_FINDER_AGENT_ID",\n'
        '    "EDGE_FINDER_ENVIRONMENT_ID",\n'
        '    "EDGE_FINDER_MEMSTORE_ID",\n'
        '    "MANAGED_AGENTS_BETA",\n'
        '    "PROVISIONED_PERSONA_SHA256",\n'
        "]\n"
    )
    IDS_MODULE_PATH.write_text(body, encoding="utf-8")


async def _provision(rebuild: bool = False) -> tuple[str, str, str]:
    """Provision (or look up) the agent + environment + return (agent_id, env_id, sha)."""
    if "ANTHROPIC_API_KEY" not in os.environ:
        raise SystemExit("ANTHROPIC_API_KEY not set — refusing to proceed.")
    persona_text, persona_sha = _read_persona()
    log.info(
        "anthropic_provision.persona_loaded",
        sha256=persona_sha,
        bytes=len(persona_text.encode("utf-8")),
    )
    async with AsyncAnthropic() as client:
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
    return agent_id, env_id, persona_sha


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Always create new agent + environment (skip lookup). Use after persona edits.",
    )
    args = parser.parse_args(argv)

    agent_id, env_id, persona_sha = asyncio.run(_provision(rebuild=args.rebuild))
    _write_ids_module(agent_id, env_id, persona_sha)
    print(f"agent_id:       {agent_id}")
    print(f"environment_id: {env_id}")
    print(f"persona_sha256: {persona_sha}")
    print(f"wrote:          {IDS_MODULE_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
