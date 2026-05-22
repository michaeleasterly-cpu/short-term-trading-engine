"""Anthropic Managed-Agents SDK wiring for the LLM-AAR critic.

Mirrors ops/llm_edge_finder_sdk.py (PR #294 pattern) — uses the
Anthropic Sessions API so the critic has stateful cross-run memory
via its dedicated memstore (aar-llm-critic-context).

Architecture:
- ONE Anthropic Session per critic run.
- ONE user→agent round-trip per session (single-turn — the critic does
  not run an analysis loop like the finder).
- The dedicated AAR memstore is attached as a memory_store resource on
  session creation; mounted at ``/mnt/memory/aar-llm-critic/``. The agent
  reads /agent-context/, /lessons/, /findings/<engine>/, /recent-runs/
  via the standard read/write/edit/glob/grep tools (enabled at agent
  provision time per persona §8).
- The Agent's server-side ``system`` field carries the persona (set at
  provision time). The application-side persona text passed in is
  asserted against PROVISIONED_PERSONA_SHA256 (defense-in-depth warn).
- No custom tools registered — the critic doesn't run statsmodels; it
  emits a JSON envelope only (the application-side validation +
  application_log writes are deterministic).

Safety contract (spec §6 + persona §9):
- NO custom tools BEYOND the default memstore file ops (bash/web
  DISABLED at agent provision).
- AuthSkip raised on AuthenticationError → caller treats as smoke mode.
- 529 / 5xx backoff preserved (15/30/60s for 429; 60/120/300s for 5xx)
  per feedback_anthropic_529_self_heal.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import structlog
from anthropic import (
    APIStatusError,
    AsyncAnthropic,
    AuthenticationError,
    InternalServerError,
    RateLimitError,
)

from ops.llm_aar_anthropic_ids import (
    AAR_CRITIC_AGENT_ID,
    AAR_CRITIC_ENVIRONMENT_ID,
    AAR_CRITIC_MEMSTORE_ID,
    MANAGED_AGENTS_BETA,
    PROVISIONED_PERSONA_SHA256,
)
from ops.llm_aar_critic import LLMCallable

if TYPE_CHECKING:  # pragma: no cover
    pass

log = structlog.get_logger(__name__)


# Default Anthropic SDK params. Model pinned at the Agent (server-side);
# kept here only for compat-name parity with finder SDK.
ANTHROPIC_MODEL = "claude-sonnet-4-6"
ANTHROPIC_MAX_TOKENS = 4096


class AuthSkip(Exception):
    """Signals invalid/exhausted Anthropic API key. Caller treats as
    'no API key, critic runs in smoke mode'."""


class AgentSessionError(RuntimeError):
    """session.error event arrived from Anthropic; surface upstream."""


# Memstore mount path derived from name 'aar-llm-critic-context'.
# Anthropic API slugifies the name → '/mnt/memory/aar-llm-critic-context/'.
# We use the shorter alias the persona references.
MEMSTORE_MOUNT_PATH = "/mnt/memory/aar-llm-critic"

MEMSTORE_ATTACH_INSTRUCTIONS = (
    "Your cross-run memory. Read /agent-context/curation-policy.md, "
    "/lessons/, /findings/<engine>/ (where <engine> is the one you're "
    "currently considering), /recent-runs/. Application writes "
    "/findings/<engine>/<finding_id>.md + /recent-runs/<run_id>.md on "
    "your behalf — you do not write here directly. See persona §8."
)


class _SessionLLMState:
    """Stateful wrapper holding the open Anthropic session for one critic run.

    The application's LLMCallable protocol is preserved — each __call__ is
    one user→agent round-trip. The critic is single-turn so this state is
    minimal (one session, one round-trip per run).
    """

    def __init__(
        self,
        client: AsyncAnthropic,
        *,
        agent_id: str = AAR_CRITIC_AGENT_ID,
        environment_id: str = AAR_CRITIC_ENVIRONMENT_ID,
        memstore_id: str = AAR_CRITIC_MEMSTORE_ID,
        title_hint: str | None = None,
    ) -> None:
        self._client = client
        self._agent_id = agent_id
        self._environment_id = environment_id
        self._memstore_id = memstore_id
        self._title_hint = title_hint or f"aar-critic-{uuid4().hex[:8]}"
        self._session_id: str | None = None

    async def ensure_session(self) -> str:
        if self._session_id is None:
            session = await self._client.beta.sessions.create(
                agent=self._agent_id,
                environment_id=self._environment_id,
                title=self._title_hint,
                resources=[
                    {
                        "type": "memory_store",
                        "memory_store_id": self._memstore_id,
                        "access": "read_write",
                        "instructions": MEMSTORE_ATTACH_INSTRUCTIONS,
                    }
                ],
                betas=[MANAGED_AGENTS_BETA],
            )
            self._session_id = session.id
            log.info(
                "llm_aar_critic_sdk.session_created",
                session_id=self._session_id,
                agent_id=self._agent_id,
                environment_id=self._environment_id,
                memstore_id=self._memstore_id,
            )
        return self._session_id

    async def send_user_message(self, session_id: str, text: str) -> None:
        await self._client.beta.sessions.events.send(
            session_id=session_id,
            events=[
                {
                    "type": "user.message",
                    "content": [{"type": "text", "text": text}],
                }
            ],
            betas=[MANAGED_AGENTS_BETA],
        )

    async def consume_until_idle(self, session_id: str) -> str:
        """Stream events until idle. Return latest agent.message text body."""
        latest_agent_text: str = ""
        stream = await self._client.beta.sessions.events.stream(
            session_id=session_id, betas=[MANAGED_AGENTS_BETA]
        )
        async for event in stream:
            event_type = getattr(event, "type", None)
            if event_type == "agent.message":
                content = getattr(event, "content", None) or []
                parts = []
                for block in content:
                    if getattr(block, "type", None) == "text":
                        parts.append(str(getattr(block, "text", "")))
                latest_agent_text = (
                    "\n".join(parts) if parts else latest_agent_text
                )
            elif event_type == "session.status_idle":
                break
            elif event_type == "session.error":
                err = getattr(event, "error", None)
                raise AgentSessionError(f"session.error: {err}")
        return latest_agent_text


def make_sdk_aar_callable(
    client: AsyncAnthropic | None = None,
    *,
    agent_id: str | None = None,
    environment_id: str | None = None,
    memstore_id: str | None = None,
    title_hint: str | None = None,
) -> LLMCallable:
    """Return an async LLM callable bound to the Anthropic Sessions API.

    Each call this factory returns is run-scoped: one Anthropic session
    per critic run; one user→agent round-trip per run. Cross-run state
    lives in the memstore.
    """
    _client = client
    _state: _SessionLLMState | None = None

    async def _call(
        system_prompt: str,
        user_prompt_first_turn: str,
        transcript: list[dict[str, Any]],
    ) -> dict[str, Any]:
        nonlocal _client, _state
        if _client is None:
            _client = AsyncAnthropic()  # lazy; needs ANTHROPIC_API_KEY
        if _state is None:
            _state = _SessionLLMState(
                _client,
                agent_id=agent_id or AAR_CRITIC_AGENT_ID,
                environment_id=environment_id or AAR_CRITIC_ENVIRONMENT_ID,
                memstore_id=memstore_id or AAR_CRITIC_MEMSTORE_ID,
                title_hint=title_hint,
            )
            _assert_persona_alignment(system_prompt)

        # Critic is single-turn — transcript is always empty when we send.
        # If for some reason a transcript arrives (defensive), append the
        # latest tool_results to the user_text the same way the finder does.
        if len(transcript) == 0:
            user_text = user_prompt_first_turn
        else:
            latest = transcript[-1]
            user_text = json.dumps({"tool_results": latest.get("tool_results", [])})

        last_exc: Exception | None = None
        agent_text = ""
        for attempt in range(4):
            try:
                session_id = await _state.ensure_session()
                await _state.send_user_message(session_id, user_text)
                agent_text = await _state.consume_until_idle(session_id)
                break
            except AuthenticationError as exc:
                raise AuthSkip(str(exc)) from None
            except RateLimitError as exc:
                last_exc = exc
                sleep_s = (15, 30, 60)[min(attempt, 2)]
                log.warning(
                    "llm_aar_critic_sdk.rate_limited_retry",
                    attempt=attempt + 1, sleep_seconds=sleep_s,
                )
                if attempt < 3:
                    await asyncio.sleep(sleep_s)
            except (InternalServerError, APIStatusError) as exc:
                status = getattr(exc, "status_code", None)
                if status is not None and status < 500:
                    raise
                last_exc = exc
                sleep_s = (60, 120, 300)[min(attempt, 2)]
                log.warning(
                    "llm_aar_critic_sdk.platform_error_retry",
                    attempt=attempt + 1, sleep_seconds=sleep_s,
                    status=status, error=str(exc)[:200],
                )
                if attempt < 3:
                    await asyncio.sleep(sleep_s)
            except AgentSessionError as exc:
                log.error(
                    "llm_aar_critic_sdk.session_error", error=str(exc)
                )
                raise
        if not agent_text and last_exc is not None:
            raise last_exc

        cleaned = _strip_markdown_fences_and_prose(agent_text)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as exc:
            log.warning(
                "llm_aar_critic_sdk.json_decode_failed",
                error=str(exc),
                preview=agent_text[:200],
            )
            # Synthesise an empty critic response so the loop completes
            # without crashing. Application logs the partial JSON for
            # diagnosis; provenance row records rejection_reason.
            preview = agent_text[:400].replace("\n", " ")
            return {
                "kind": "AARCriticResponse",
                "findings": [],
                "rationale": (
                    f"json_decode_failure: {exc.__class__.__name__}: "
                    f"{str(exc)[:100]}. raw preview: {preview}"
                )[:200],
            }

    return _call


# ───────────────────────── Helpers ─────────────────────────


def _assert_persona_alignment(system_prompt: str) -> None:
    """Verify in-process persona matches the SHA pinned at provision time.

    Soft check (warning-not-raise) — same discipline as finder SDK. The
    server-side Agent.system is the binding text; in-process drift
    indicates the persona was edited without re-running
    scripts/anthropic_aar_critic_provision.py --rebuild.
    """
    if PROVISIONED_PERSONA_SHA256 == "PENDING_PROVISION":
        # Pre-provision state — skip the check entirely.
        return
    sha = hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()
    if sha != PROVISIONED_PERSONA_SHA256:
        log.warning(
            "llm_aar_critic_sdk.persona_sha_mismatch",
            in_process_sha=sha,
            provisioned_sha=PROVISIONED_PERSONA_SHA256,
            hint=(
                "Re-run scripts/anthropic_aar_critic_provision.py "
                "--rebuild if persona was edited."
            ),
        )


def _strip_markdown_fences_and_prose(text: str) -> str:
    """Strip ```json ... ``` fences + pre-/post-JSON prose.

    Same heuristic as the finder SDK: find first '{' and last '}' →
    return that substring.
    """
    if not text:
        return text
    if "```" in text:
        start = text.find("```")
        rest = text[start + 3:]
        if rest.startswith("json"):
            rest = rest[4:]
        rest = rest.lstrip("\n")
        end = rest.find("```")
        if end >= 0:
            text = rest[:end]
        else:
            text = rest
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace >= 0 and last_brace > first_brace:
        return text[first_brace:last_brace + 1]
    return text


__all__ = [
    "ANTHROPIC_MAX_TOKENS",
    "ANTHROPIC_MODEL",
    "AgentSessionError",
    "AuthSkip",
    "MEMSTORE_ATTACH_INSTRUCTIONS",
    "MEMSTORE_MOUNT_PATH",
    "make_sdk_aar_callable",
]
