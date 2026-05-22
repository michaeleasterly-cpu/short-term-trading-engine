"""Anthropic Managed-Agents SDK wiring for the LLM edge-finder — Task #25 + 2026-05-22 Sessions API.

Binds the ``LLMCallable`` seam in ``ops/llm_edge_finder.py`` to the
Anthropic Managed-Agents Sessions API so the finder has stateful
cross-run memory via its dedicated memstore.

Architecture (cf. ``docs/superpowers/plans/2026-05-22-finder-anthropic-sessions-api.md``):
- ONE Anthropic ``Session`` per finder ``run`` (not per ``_call``).
- Each ``_call`` invocation that the application loop makes is a SINGLE
  user→agent round-trip on that session: send a ``user.message`` event,
  consume events until ``session.status_idle``, parse the latest
  ``agent.message`` content (text-only — we treat its body as a JSON
  envelope), return the decoded dict to the application.
- The memstore (``memstore_01MzLun3AfRf2viPmDqJvsWi``) is attached as a
  ``memory_store`` resource on session creation, mounted at
  ``/mnt/memory/lab-finder/``. The agent reads/writes via the standard
  ``read``/``write``/``edit``/``glob``/``grep`` tools enabled on the
  Agent's toolset. The application does NOT need a custom memory tool.
- The Agent's server-side ``system`` field carries the persona. The
  ``system_prompt`` arg passed into ``LLMCallable`` is asserted to match
  ``PROVISIONED_PERSONA_SHA256`` (defense-in-depth — drift = config error).
- The application-side ``tool_sandbox`` (``OLS_HAC_NW``,
  ``cost_net_simulation`` etc.) is UNCHANGED — the agent emits a JSON
  envelope with ``tool_calls`` (text body); the outer loop dispatches
  them; results go back as the next ``user.message``. The advisory-only
  contract (persona §2.8) is preserved.

Safety contract (per spec §2 + the 2026-05-22 update):
- NO custom ``tools`` registered with the Anthropic Agent BEYOND the
  default toolset's file ops — bash/web are DISABLED at agent provision
  time, enforced in ``scripts/anthropic_agent_provision.py``.
- ``AuthSkip`` raised on auth error → caller treats as "no API key,
  finder runs in smoke mode".
- 529 / 5xx backoff preserved (15/30/60s for 429; 60/120/300s for 5xx).
- Each finder run = ONE session creation = one fixed-cost path; per-turn
  invocation is just ``events.send`` + ``events.stream``.

The ``make_sdk_llm_callable`` factory returns an ``LLMCallable`` whose
internal state is scoped to a single finder run. The application
constructs ONE callable per ``run_finder`` invocation; cross-run state
lives in the memstore, not in the in-process wrapper.
"""
from __future__ import annotations

import asyncio
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

from ops.llm_edge_finder import LLMCallable
from ops.llm_finder_anthropic_ids import (
    EDGE_FINDER_AGENT_ID,
    EDGE_FINDER_ENVIRONMENT_ID,
    EDGE_FINDER_MEMSTORE_ID,
    MANAGED_AGENTS_BETA,
    PROVISIONED_PERSONA_SHA256,
)

if TYPE_CHECKING:  # pragma: no cover
    pass

log = structlog.get_logger(__name__)


# Default Anthropic SDK params. Model is now pinned at the Agent (server-side);
# kept here as compat-name only for older test imports.
ANTHROPIC_MODEL = "claude-sonnet-4-6"
ANTHROPIC_MAX_TOKENS = 2048


class AuthSkip(Exception):
    """Signals that the Anthropic API key is invalid/exhausted
    (AuthenticationError). Caller treats as "no API key, finder runs in
    smoke mode" — safe no-op, zero retries."""


# Finder needs larger output budget than data-triage. Full AnalysisResult
# with 1-3 ProposedSpecs + falsification criteria + evidence refs +
# rationale CAN exceed 8000 chars at the structural caps.
EDGE_FINDER_MAX_TOKENS: int = 8192

# Memstore mount path inside the agent's session container — derived
# from the memstore's name (``lab-finder``). The persona references
# ``/mnt/memory/lab-finder/`` so the agent uses absolute paths.
MEMSTORE_MOUNT_PATH = "/mnt/memory/lab-finder"

# Per-attachment instructions for the memstore, rendered into the agent's
# server-side memory section. Brief — the full discipline is in the
# persona's §11 + the curation-policy.md entry seeded into the memstore.
MEMSTORE_ATTACH_INSTRUCTIONS = (
    "Your cross-run memory. Read /agent-context/, /cross-agent/dev-to-finder/, "
    "/prior-emissions/, /outcomes/, /lessons/ at startup. Write "
    "/sessions/<run_id>.md before emitting AnalysisResult. See persona §11 + "
    "/agent-context/curation-policy.md for the full discipline."
)


class _SessionLLMState:
    """Stateful wrapper holding the open Anthropic session for one finder run.

    The application's ``LLMCallable`` protocol is preserved — each ``__call__``
    is one user→agent round-trip. Internally we open a session on the first
    call, send subsequent ``user.message`` events on later calls, and
    consume the event stream until the session goes idle each turn.

    Lifetime: one instance per ``run_finder`` invocation. Garbage-collected
    when the run completes; the Anthropic session lives on Anthropic's
    side and is implicitly closed by inactivity (or explicitly archived if
    we add a teardown call later — currently NOT done to keep the surface
    minimal; archive-on-completion is an optional polish for a follow-up).
    """

    def __init__(
        self,
        client: AsyncAnthropic,
        *,
        agent_id: str = EDGE_FINDER_AGENT_ID,
        environment_id: str = EDGE_FINDER_ENVIRONMENT_ID,
        memstore_id: str = EDGE_FINDER_MEMSTORE_ID,
        title_hint: str | None = None,
    ) -> None:
        self._client = client
        self._agent_id = agent_id
        self._environment_id = environment_id
        self._memstore_id = memstore_id
        self._title_hint = title_hint or f"edge-finder-{uuid4().hex[:8]}"
        self._session_id: str | None = None
        # The application's outer loop appends entries to the transcript
        # list each turn; we track its length so each ``__call__`` knows
        # whether this is the first turn (empty transcript) or a follow-up
        # (transcript grew since last call → send the latest tool_results
        # as the next user.message).
        self._last_seen_transcript_len = 0

    async def _ensure_session(self) -> str:
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
                "llm_edge_finder_sdk.session_created",
                session_id=self._session_id,
                agent_id=self._agent_id,
                environment_id=self._environment_id,
                memstore_id=self._memstore_id,
            )
        return self._session_id

    async def _send_user_message(self, session_id: str, text: str) -> None:
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

    async def _consume_until_idle(self, session_id: str) -> str:
        """Stream events until the session goes idle. Return the latest
        ``agent.message`` text body. If multiple agent.message events fire
        in one turn (which can happen with thinking blocks), the LAST one
        wins — that's our AnalysisRequest / AnalysisResult envelope.

        Idle conditions handled:
        - ``session.status_idle`` with ``end_turn``: normal completion.
        - ``session.status_idle`` with ``retries_exhausted``: surfaces as
          empty body → caller's JSON-decode fallback emits the synthetic
          AnalysisRequest.
        - ``session.status_idle`` with ``requires_action``: would need a
          tool confirmation event. The current toolset uses
          ``always_allow`` so this is unreachable; if it fires we treat
          as terminal + return empty body.
        - ``session.error``: raises ``AgentSessionError`` (caller logs +
          surfaces upstream).
        """
        latest_agent_text: str = ""
        stream = await self._client.beta.sessions.events.stream(
            session_id=session_id, betas=[MANAGED_AGENTS_BETA]
        )
        async for event in stream:
            event_type = getattr(event, "type", None)
            if event_type == "agent.message":
                # Content is a list of text blocks; we concatenate. Most
                # turns are a single text block; thinking emits a
                # separate agent.thinking event we ignore.
                content = getattr(event, "content", None) or []
                parts = []
                for block in content:
                    if getattr(block, "type", None) == "text":
                        parts.append(str(getattr(block, "text", "")))
                latest_agent_text = "\n".join(parts) if parts else latest_agent_text
            elif event_type == "session.status_idle":
                # Idle event — done with this turn. Stop reading the stream.
                break
            elif event_type == "session.error":
                err = getattr(event, "error", None)
                raise AgentSessionError(f"session.error: {err}")
            # All other event types (agent.tool_use against the file tools,
            # agent.tool_result, span.* tracing, session.status_running)
            # are informational — the agent's memstore reads/writes flow
            # through them but we don't need to act on them here.
        return latest_agent_text


class AgentSessionError(RuntimeError):
    """A ``session.error`` event arrived from Anthropic; surface upstream."""


def make_sdk_llm_callable(
    client: AsyncAnthropic | None = None,
    *,
    max_tokens: int = EDGE_FINDER_MAX_TOKENS,  # noqa: ARG001 — preserved for compat
    agent_id: str | None = None,
    environment_id: str | None = None,
    memstore_id: str | None = None,
    title_hint: str | None = None,
) -> LLMCallable:
    """Return an async LLM callable bound to the Anthropic Sessions API.

    Each call this factory returns is run-scoped: it holds ONE Anthropic
    session across the multi-turn application loop in
    ``ops.llm_edge_finder._drive_llm_loop``. Cross-run state lives in the
    memstore, not in this wrapper.

    Args:
        client: pre-constructed AsyncAnthropic. When None, the wrapper
            defers construction to first use (tests monkey-patch).
        max_tokens: kept for compat with the v1 messages-API signature;
            unused under Sessions (the Agent's server-side config wins).
        agent_id: defaults to the provisioned ``EDGE_FINDER_AGENT_ID``.
        environment_id: defaults to ``EDGE_FINDER_ENVIRONMENT_ID``.
        memstore_id: defaults to ``EDGE_FINDER_MEMSTORE_ID``.
        title_hint: optional session title for the Anthropic dashboard.

    Returns:
        An async callable matching the ``LLMCallable`` protocol:
        ``(system_prompt, user_prompt_first_turn, transcript) -> dict``
        where the dict is the decoded JSON envelope per spec §3.2.
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
            _client = AsyncAnthropic()  # lazy construction; needs ANTHROPIC_API_KEY
        if _state is None:
            _state = _SessionLLMState(
                _client,
                agent_id=agent_id or EDGE_FINDER_AGENT_ID,
                environment_id=environment_id or EDGE_FINDER_ENVIRONMENT_ID,
                memstore_id=memstore_id or EDGE_FINDER_MEMSTORE_ID,
                title_hint=title_hint,
            )
            # Defense-in-depth: the application's system_prompt MUST match
            # the persona pinned at agent provision time. Drift = config
            # error (the operator edited the persona without re-provisioning).
            _assert_persona_alignment(system_prompt)

        # Compose the user message text for THIS turn.
        if len(transcript) == 0:
            # First turn — send the application's first-turn user prompt.
            # This contains the snapshot summary + bundles + tool whitelist.
            user_text = user_prompt_first_turn
        else:
            # Follow-up — send the tool_results from the latest transcript
            # entry. The agent's prior turn emitted a JSON envelope with
            # tool_calls; the application dispatched them; we feed the
            # results back as the next user.message.
            latest = transcript[-1]
            user_text = json.dumps({"tool_results": latest.get("tool_results", [])})

        # Retry on transient Anthropic platform errors with backoff.
        # Same shape as the v1 messages-API path (15/30/60s for 429;
        # 60/120/300s for 5xx). Per feedback_anthropic_529_self_heal.
        last_exc: Exception | None = None
        agent_text = ""
        for attempt in range(4):  # initial + 3 retries
            try:
                session_id = await _state._ensure_session()
                await _state._send_user_message(session_id, user_text)
                agent_text = await _state._consume_until_idle(session_id)
                # Account for the fact that the application appends a new
                # transcript entry AFTER this call returns; bump our
                # last-seen length so the next call's mode decision is
                # transcript-len-relative (defensive, not load-bearing
                # under current loop semantics).
                _state._last_seen_transcript_len = len(transcript) + 1
                break
            except AuthenticationError as exc:
                raise AuthSkip(str(exc)) from None
            except RateLimitError as exc:
                last_exc = exc
                sleep_s = (15, 30, 60)[min(attempt, 2)]
                log.warning(
                    "llm_edge_finder_sdk.rate_limited_retry",
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
                    "llm_edge_finder_sdk.platform_error_retry",
                    attempt=attempt + 1, sleep_seconds=sleep_s,
                    status=status, error=str(exc)[:200],
                )
                if attempt < 3:
                    await asyncio.sleep(sleep_s)
            except AgentSessionError as exc:
                # Session-level errors are NOT transient — fail loud.
                log.error("llm_edge_finder_sdk.session_error", error=str(exc))
                raise
        if not agent_text and last_exc is not None:
            raise last_exc

        cleaned = _strip_markdown_fences_and_prose(agent_text)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as exc:
            log.warning(
                "llm_edge_finder_sdk.json_decode_failed",
                error=str(exc),
                preview=agent_text[:200],
            )
            preview = agent_text[:400].replace("\n", " ")
            return {
                "kind": "AnalysisRequest",
                "rationale": (
                    f"Your previous response was not valid JSON "
                    f"(error: {exc.__class__.__name__}: {str(exc)[:100]}). "
                    f"You returned: '{preview}'. "
                    f"You MUST respond with ONLY a JSON object — no prose, "
                    f"no markdown fences, no preamble. Start your next "
                    f"response with '{{'."
                ),
                "tool_calls": [],
            }

    return _call


# ───────────────────────── Helpers ─────────────────────────


def _assert_persona_alignment(system_prompt: str) -> None:
    """Verify the in-process persona text matches the SHA pinned at agent
    provision time. Drift = the operator edited the persona without
    re-running ``scripts/anthropic_agent_provision.py --rebuild`` — the
    Anthropic Agent's server-side ``system`` field is now stale.

    This is a soft check (logs warning, does NOT raise) because the
    smoke-test path constructs the callable with a synthetic persona;
    full assertion is reserved for the live pilot.
    """
    import hashlib
    sha = hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()
    if sha != PROVISIONED_PERSONA_SHA256:
        log.warning(
            "llm_edge_finder_sdk.persona_sha_mismatch",
            in_process_sha=sha,
            provisioned_sha=PROVISIONED_PERSONA_SHA256,
            hint=(
                "Re-run scripts/anthropic_agent_provision.py --rebuild if "
                "the persona file was edited; pilot results from this "
                "session use the SERVER-SIDE persona, not the in-process one."
            ),
        )


def _strip_markdown_fences_and_prose(text: str) -> str:
    """Strip ```json ... ``` fences + any pre-/post-JSON prose.

    Common LLM artifacts:
    - "```json\\n{...}\\n```"
    - "Here is the JSON:\\n{...}"
    - "{...}\\n\\nLet me know if you need more!"

    Heuristic: find the first '{' and last '}'; return the substring.
    If those aren't found, return the original text (json.loads will
    then fail loudly with the original input preserved).
    """
    if not text:
        return text
    if "```" in text:
        start = text.find("```")
        rest = text[start + 3 :]
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
        return text[first_brace : last_brace + 1]
    return text


__all__ = [
    "ANTHROPIC_MAX_TOKENS",
    "ANTHROPIC_MODEL",
    "AgentSessionError",
    "AuthSkip",
    "MEMSTORE_ATTACH_INSTRUCTIONS",
    "MEMSTORE_MOUNT_PATH",
    "make_sdk_llm_callable",
]
