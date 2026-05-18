"""LLM data triage agent (LT-P2) — Advisory only, NEVER acts.

Calls the official Anthropic SDK to produce a non-authoritative
DATA_LLM_TRIAGE_PROPOSAL for each genuinely novel data escalation.
Mocked in CI; no live API calls in automated pipelines. Lands dark —
not wired into any cycle until P3 (the human-review / PR step).

Safety boundary: the deterministic CI fence (provenance + hard-denied
paths + post-merge canary), NOT this module. This module only governs
output quality via the advisory persona.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import structlog
from anthropic import (
    Anthropic,
    APIError,
    AuthenticationError,
    RateLimitError,
)

from tpcore.llm_data_triage import PERSONA_VERSION as _PERSONA_VERSION
from tpcore.llm_data_triage.packet import TriagePacket, build_packet
from tpcore.llm_data_triage.select import select_novel_escalations
from tpcore.outage import with_retry

logger = structlog.get_logger(__name__)

# Private sentinel: raised inside _call_api when the SDK raises
# AuthenticationError so the exception escapes with_retry's retry_on
# tuple WITHOUT triggering any retry loop. with_retry gates retries via
# ``except retry_on as exc`` (isinstance check, retry.py:130); _AuthSkip
# is NOT in retry_on, so it propagates immediately.
class _AuthSkip(Exception):
    """Signals that the Anthropic API key is invalid/exhausted (AuthenticationError).
    Treated identically to a missing key: safe no-op, zero retries, zero emits."""

_MODEL = "claude-sonnet-4-6"
_MAX_TOKENS = 2048
_PERSONA_PATH = (
    pathlib.Path(__file__).resolve().parents[1] / "docs" / "llm_data_triage_persona.md"
)

# Engine tag emitted to application_log — matches data_repair_service convention.
_AGENT_ENGINE_TAG = "llm_data_triage"

# Cached at import time — file is static for the lifetime of the process.
_PERSONA_TEXT: str = _PERSONA_PATH.read_text(encoding="utf-8")


def _persona() -> str:
    return _PERSONA_TEXT


# Mirror data_repair_service._INSERT_SQL exactly (same table, same column order,
# same ::jsonb cast convention from tpcore/logging/db_handler.py).
_INSERT_SQL = """
INSERT INTO platform.application_log
    (engine, run_id, event_type, severity, message, data)
VALUES
    ($1, $2, $3, $4, $5, $6::jsonb)
"""


async def _emit(
    pool: Any,
    event_type: str,
    message: str,
    data: dict[str, Any],
    *,
    severity: str = "INFO",
) -> None:
    """Insert one advisory/operational event. ``data`` is json.dumps'd
    to a string and cast to jsonb DB-side (db_handler convention)."""
    async with pool.acquire() as conn:
        await conn.execute(
            _INSERT_SQL,
            _AGENT_ENGINE_TAG,
            uuid.uuid4(),
            event_type,
            severity,
            message,
            json.dumps(data, default=str),
        )


@dataclass
class TriageOutcome:
    proposed: list[str] = field(default_factory=list)
    skipped_no_key: bool = False
    error: str | None = None


def _default_client() -> Anthropic:
    return Anthropic()


async def run_triage(
    pool: Any,
    *,
    client_factory: Callable[[], Any] = _default_client,
) -> TriageOutcome:
    """Run one advisory triage pass. Never raises — all failures are
    captured in ``TriageOutcome.error``. Never emits on failure."""
    out = TriageOutcome()
    try:  # noqa: BLE001 — never abort the sweep
        if not os.environ.get("ANTHROPIC_API_KEY"):
            logger.info("llm_data_triage.no_api_key")
            out.skipped_no_key = True
            return out

        escs = await select_novel_escalations(pool)
        if not escs:
            logger.info("llm_data_triage.no_novel_escalations")
            return out

        client = client_factory()

        @with_retry(
            max_attempts=3,
            backoff_base_sec=2.0,
            backoff_cap_sec=30.0,
            retry_on=(RateLimitError, APIError),
        )
        async def _call_api(pkt_arg: TriagePacket) -> Any:
            """Thin async wrapper so @with_retry (async decorator) applies.

            The synchronous SDK call is wrapped here — mirrors the
            @with_retry pattern on edgar_adapter / fred adapter call sites.
            pkt_arg is passed explicitly to avoid B023 loop-variable capture.

            AuthenticationError is intercepted here and re-raised as
            _AuthSkip so it escapes the retry_on tuple entirely (zero
            retries, zero backoff delays). See _AuthSkip docstring.
            """
            try:
                return client.messages.create(
                    model=_MODEL,
                    max_tokens=_MAX_TOKENS,
                    temperature=0.0,
                    system=_persona(),
                    messages=[{"role": "user", "content": pkt_arg.text}],
                )
            except AuthenticationError:
                raise _AuthSkip() from None

        for esc in escs:
            pkt = await build_packet(pool, esc)

            try:
                resp = await _call_api(pkt)
            except _AuthSkip:
                logger.warning("llm_data_triage.auth_error_skipped", ref=esc.ref)
                out.skipped_no_key = True
                return out
            except Exception as call_exc:  # noqa: BLE001 — isolate per-escalation
                logger.error(
                    "llm_data_triage.call_error",
                    ref=esc.ref,
                    error=str(call_exc),
                )
                raise  # propagate to outer try/except → sets out.error

            try:
                txt = resp.content[0].text
                try:
                    prop = json.loads(txt)
                except json.JSONDecodeError as json_exc:
                    logger.warning(
                        "llm_data_triage.malformed_response",
                        ref=esc.ref,
                        error=str(json_exc),
                        response_preview=txt[:200],
                    )
                    continue  # skip this escalation, don't abort the loop

                if not isinstance(prop, dict):
                    logger.warning(
                        "llm_data_triage.non_dict_response",
                        ref=esc.ref,
                        response_preview=txt[:200],
                    )
                    continue  # skip this escalation, don't abort the loop

                await _emit(
                    pool,
                    "DATA_LLM_TRIAGE_PROPOSAL",
                    f"LLM triage proposal for ref={esc.ref}",
                    {
                        "schema": 1,
                        "ref": esc.ref,
                        "cls": esc.cls,
                        "persona_version": _PERSONA_VERSION,
                        "model": _MODEL,
                        "proposed_disposition": prop.get("proposed_disposition"),
                        "confidence": prop.get("confidence"),
                        "rationale": prop.get("rationale"),
                        "could_not_determine": prop.get("could_not_determine"),
                        "packet_hash": pkt.packet_hash,
                        "usage": {
                            "in": resp.usage.input_tokens,
                            "out": resp.usage.output_tokens,
                        },
                    },
                )
                out.proposed.append(esc.ref)
                logger.info(
                    "llm_data_triage.proposal_emitted",
                    ref=esc.ref,
                    model=_MODEL,
                    persona_version=_PERSONA_VERSION,
                )
            except (IndexError, AttributeError, KeyError, TypeError) as parse_exc:
                logger.warning(
                    "llm_data_triage.malformed_response",
                    ref=esc.ref,
                    error=str(parse_exc),
                )
                continue  # skip this escalation, don't abort the loop

    except Exception as exc:  # noqa: BLE001 — never raises; crash-isolated
        logger.error("llm_data_triage.error", error=str(exc))
        out.error = str(exc)

    return out


__all__ = ["TriageOutcome", "run_triage"]


def main() -> None:  # pragma: no cover
    import os as _os

    from tpcore.db import build_asyncpg_pool

    async def _amain() -> None:
        dsn = _os.environ.get("DATABASE_URL") or _os.environ.get("DATABASE_URL_IPV4")
        if not dsn:
            logger.error("llm_data_triage.no_dsn")
            sys.exit(1)
        pool = await build_asyncpg_pool(dsn)
        try:
            result = await run_triage(pool)
        finally:
            await pool.close()
        print(
            f"llm_data_triage: proposed={result.proposed} "
            f"skipped_no_key={result.skipped_no_key} error={result.error}"
        )

    asyncio.run(_amain())


if __name__ == "__main__":  # pragma: no cover
    main()
