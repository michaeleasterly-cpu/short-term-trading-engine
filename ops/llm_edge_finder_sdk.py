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

import asyncio
import json
from typing import TYPE_CHECKING, Any

import structlog
from anthropic import AsyncAnthropic, AuthenticationError, RateLimitError

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


# Finder needs larger output budget than data-triage. Full AnalysisResult
# with 1-3 ProposedSpecs + falsification criteria + evidence refs +
# rationale CAN exceed 8000 chars at the structural caps: each spec is
# up to 4096 (rationale) + 2048 (hypothesis) + 2048 (falsification) =
# ~8k chars; finder_rationale max 8192. Sonnet 4.6 caps at 8192 output
# tokens. We use the max + the prompt nudges for ONE best candidate
# rather than 3 to fit reliably.
EDGE_FINDER_MAX_TOKENS: int = 8192


def make_sdk_llm_callable(
    client: AsyncAnthropic | None = None,
    *,
    model: str = ANTHROPIC_MODEL,
    max_tokens: int = EDGE_FINDER_MAX_TOKENS,
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

        # Prompt caching — 90% discount on cached input tokens after
        # turn 1 (Anthropic docs: cache_control ephemeral, 5m TTL).
        # The persona (system) + bundles+tool-whitelist (first user
        # turn) are stable across turns + are the biggest token sink;
        # caching them cuts per-pilot cost ~3-4x AND eliminates the
        # 30k-tokens/min input-rate-limit pressure.
        system_blocks = _system_blocks_with_cache(system_prompt)
        messages = _build_messages_with_cache(user_prompt_first_turn, transcript)
        # Retry on 429 (rate limit) with exponential backoff. Caching
        # should make 429s extremely rare on this path, but the safety
        # net stays.
        backoff_seconds = (15, 30, 60)
        last_exc: Exception | None = None
        response = None
        for attempt, sleep_s in enumerate((0,) + backoff_seconds):
            if sleep_s:
                log.warning(
                    "llm_edge_finder_sdk.rate_limited_retry",
                    attempt=attempt,
                    sleep_seconds=sleep_s,
                )
                await asyncio.sleep(sleep_s)
            try:
                response = await _client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    temperature=0.0,
                    system=system_blocks,
                    messages=messages,
                )
                # Surface cache stats so we can audit savings.
                usage = getattr(response, "usage", None)
                if usage is not None:
                    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
                    cache_create = getattr(usage, "cache_creation_input_tokens", 0) or 0
                    input_tokens = getattr(usage, "input_tokens", 0) or 0
                    log.info(
                        "llm_edge_finder_sdk.usage",
                        input=input_tokens,
                        cache_read=cache_read,
                        cache_create=cache_create,
                        output=getattr(usage, "output_tokens", 0) or 0,
                    )
                break
            except AuthenticationError as exc:
                raise AuthSkip(str(exc)) from None
            except RateLimitError as exc:
                last_exc = exc
                continue
        if response is None:
            raise last_exc or RuntimeError("LLM call failed without exception")

        text = _extract_text(response)
        cleaned = _strip_markdown_fences_and_prose(text)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as exc:
            # Surface the decode error AS a synthetic AnalysisRequest. Include
            # the raw text (truncated) in the rationale so the LLM sees its
            # own malformed output in the next turn's transcript + can
            # correct course.
            log.warning(
                "llm_edge_finder_sdk.json_decode_failed",
                error=str(exc),
                preview=text[:200],
            )
            preview = text[:400].replace("\n", " ")
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


def _system_blocks_with_cache(system_prompt: str) -> list[dict[str, Any]]:
    """Wrap the system prompt as a single text block with ephemeral cache.

    The persona is ~2.5k tokens — well over the 1024-token caching floor
    for Sonnet. 5m TTL is default; sufficient for typical pilot runs.
    """
    return [
        {
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral", "ttl": "5m"},
        }
    ]


def _build_messages_with_cache(
    user_prompt_first_turn: str, transcript: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Compose the messages array with the first user prompt cached.

    First-turn user content (snapshot summary + bundles + tool whitelist)
    is ~10k tokens — easily exceeds the 1024-token caching floor. The
    transcript turns are NOT cached (they grow per turn + are unique).
    """
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": user_prompt_first_turn,
                    "cache_control": {"type": "ephemeral", "ttl": "5m"},
                }
            ],
        }
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
        # Tool results back to the LLM (not cached — vary every turn):
        messages.append({
            "role": "user",
            "content": json.dumps({
                "tool_results": turn_entry.get("tool_results", []),
            }),
        })
    return messages


def _build_messages(
    user_prompt_first_turn: str, transcript: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Backwards-compat wrapper used by existing tests; delegates to the
    cache-aware builder. New callers should use _build_messages_with_cache
    directly so the contract is clear."""
    return _build_messages_with_cache(user_prompt_first_turn, transcript)


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
    # Strip ```json fence if present
    if "```" in text:
        # Find the first ``` then either consume "json" + newline OR newline
        start = text.find("```")
        # Move past the first fence + optional language tag
        rest = text[start + 3 :]
        if rest.startswith("json"):
            rest = rest[4:]
        rest = rest.lstrip("\n")
        # Find closing ```
        end = rest.find("```")
        if end >= 0:
            text = rest[:end]
        else:
            text = rest
    # Find outer JSON object boundaries
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace >= 0 and last_brace > first_brace:
        return text[first_brace : last_brace + 1]
    return text


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
