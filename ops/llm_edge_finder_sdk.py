"""Anthropic SDK wiring for the LLM edge-finder — Task #25 T9 + spec §3.2.

Binds the ``LLMCallable`` seam in ``ops/llm_edge_finder.py`` to the real
Anthropic ``AsyncAnthropic.messages.create`` call. Reuses the shared
SDK surface from ``ops.llm_data_triage`` (``ANTHROPIC_MODEL``,
``ANTHROPIC_MAX_TOKENS``, ``AuthSkip``) — the same posture #187
established for the lab emitter (PR #152).

Safety contract (per spec §2 + ``.claude/rules/llm-triage.md``):
- NO ``tools`` param to the SDK — advisory text only.
- ``temperature=0.0`` for deterministic-as-possible replies.
- ``system`` = persona text; ``messages`` = user-prompt + transcript
  rounds.
- ``AuthSkip`` raised on auth error → caller treats as "no API key,
  finder runs in smoke mode" (mirrors existing triage pattern).

The `make_sdk_llm_callable` factory returns an `LLMCallable` compatible
with `ops.llm_edge_finder.run_finder(..., llm_callable=...)`.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import structlog
from anthropic import AsyncAnthropic, AuthenticationError

from ops.llm_edge_finder import LLMCallable

if TYPE_CHECKING:  # pragma: no cover
    pass

log = structlog.get_logger(__name__)


# Reuse the shared SDK surface from #187 (ops.llm_data_triage public symbols).
# Defensive: tolerate test environments where the triage module's lazy
# import path has been stubbed.
try:
    from ops.llm_data_triage import (
        ANTHROPIC_MAX_TOKENS,
        ANTHROPIC_MODEL,
        AuthSkip,
    )
except Exception:  # pragma: no cover - defensive
    ANTHROPIC_MODEL = "claude-sonnet-4-6"
    ANTHROPIC_MAX_TOKENS = 2048

    class AuthSkip(Exception):
        """Local fallback if the shared symbol is unreachable."""


def make_sdk_llm_callable(
    client: AsyncAnthropic | None = None,
    *,
    model: str = ANTHROPIC_MODEL,
    max_tokens: int = ANTHROPIC_MAX_TOKENS,
) -> LLMCallable:
    """Return an async LLM callable bound to the Anthropic SDK.

    Args:
        client: optional pre-constructed AsyncAnthropic instance.
            When None, the callable defers construction to first use
            (allowing tests to monkey-patch).
        model: model id (default = shared `ANTHROPIC_MODEL` from #187).
        max_tokens: max tokens per turn (default = shared `ANTHROPIC_MAX_TOKENS`).

    Returns:
        An async callable matching the `LLMCallable` protocol:
        `(system_prompt, user_prompt_first_turn, transcript) -> dict`
        where the dict is the decoded JSON envelope per spec §3.2.
    """
    _client = client

    async def _call(
        system_prompt: str,
        user_prompt_first_turn: str,
        transcript: list[dict[str, Any]],
    ) -> dict[str, Any]:
        nonlocal _client
        if _client is None:
            _client = AsyncAnthropic()  # lazy construction; needs ANTHROPIC_API_KEY

        messages = _build_messages(user_prompt_first_turn, transcript)
        try:
            response = await _client.messages.create(
                model=model,
                max_tokens=max_tokens,
                temperature=0.0,
                system=system_prompt,
                messages=messages,
            )
        except AuthenticationError as exc:
            raise AuthSkip(str(exc)) from None

        text = _extract_text(response)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            # Surface the decode error AS a synthetic AnalysisRequest so the
            # agent's loop can keep going (operator audits via §12 dashboard).
            log.warning("llm_edge_finder_sdk.json_decode_failed", error=str(exc))
            return {
                "kind": "AnalysisRequest",
                "rationale": f"llm_json_decode_failed: {exc.__class__.__name__}",
                "tool_calls": [],
            }

    return _call


# ───────────────────────── Helpers ─────────────────────────


def _build_messages(
    user_prompt_first_turn: str, transcript: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Compose the messages array from the first-turn prompt + transcript.

    The transcript carries per-turn (rationale, tool_calls, tool_results)
    rounds — we serialize each into a user/assistant pair so the LLM sees
    its prior turn + the tool results in standard chat format.
    """
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": user_prompt_first_turn}
    ]
    for turn_entry in transcript:
        # Prior LLM emission (AnalysisRequest):
        messages.append({
            "role": "assistant",
            "content": json.dumps({
                "kind": "AnalysisRequest",
                "rationale": turn_entry.get("rationale", ""),
                "tool_calls": turn_entry.get("tool_calls", []),
            }),
        })
        # Tool results back to the LLM:
        messages.append({
            "role": "user",
            "content": json.dumps({
                "tool_results": turn_entry.get("tool_results", []),
            }),
        })
    return messages


def _extract_text(response: Any) -> str:
    """Extract the text body from an Anthropic SDK response."""
    if hasattr(response, "content") and response.content:
        # response.content is a list of ContentBlock; first text block wins.
        for block in response.content:
            if getattr(block, "type", None) == "text":
                return str(block.text)
    return ""


__all__ = [
    "ANTHROPIC_MAX_TOKENS",
    "ANTHROPIC_MODEL",
    "AuthSkip",
    "make_sdk_llm_callable",
]
