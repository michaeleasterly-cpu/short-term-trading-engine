"""Anthropic SDK wiring tests for the LLM edge-finder — Task #25 T9.

Covers:
- make_sdk_llm_callable returns an async callable
- AuthSkip raised on Anthropic AuthenticationError (no API key path)
- JSON-decode failure → synthetic AnalysisRequest (loop continues, no crash)
- Transcript → messages array shape
- NO `tools` param in the SDK call (advisory contract)
- temperature=0.0 in the SDK call
"""
from __future__ import annotations

import json
from typing import Any

import pytest

pytestmark = pytest.mark.xdist_group("ops_shadow")


class _FakeBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.content = [_FakeBlock(text)]


class _FakeMessages:
    def __init__(self, response_text: str, capture: list[dict[str, Any]] | None = None) -> None:
        self.response_text = response_text
        self.capture = capture if capture is not None else []

    async def create(self, **kwargs: Any) -> _FakeResponse:
        self.capture.append(kwargs)
        return _FakeResponse(self.response_text)


class _FakeAnthropicClient:
    def __init__(self, response_text: str = '{"kind":"AnalysisResult","proposed_specs":[],"finder_rationale":"x"}',
                 capture: list[dict[str, Any]] | None = None) -> None:
        self.messages = _FakeMessages(response_text, capture)


class _AuthErrorMessages:
    async def create(self, **kwargs: Any) -> Any:
        import httpx
        from anthropic import AuthenticationError
        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        response = httpx.Response(401, request=request)
        raise AuthenticationError(
            message="invalid api key",
            response=response,
            body={"error": "invalid_api_key"},
        )


class _AuthErrorClient:
    def __init__(self) -> None:
        self.messages = _AuthErrorMessages()


# ───────────────────────── make_sdk_llm_callable ─────────────────────────


@pytest.mark.asyncio
async def test_make_sdk_llm_callable_returns_async_callable() -> None:
    from ops.llm_edge_finder_sdk import make_sdk_llm_callable

    callable_ = make_sdk_llm_callable(client=_FakeAnthropicClient())  # type: ignore[arg-type]
    assert callable(callable_)


@pytest.mark.asyncio
async def test_sdk_llm_callable_returns_json_envelope() -> None:
    from ops.llm_edge_finder_sdk import make_sdk_llm_callable

    callable_ = make_sdk_llm_callable(
        client=_FakeAnthropicClient(  # type: ignore[arg-type]
            response_text='{"kind":"AnalysisResult","proposed_specs":[],"finder_rationale":"ok"}',
        )
    )
    result = await callable_("sys", "user", [])
    assert result == {"kind": "AnalysisResult", "proposed_specs": [], "finder_rationale": "ok"}


@pytest.mark.asyncio
async def test_sdk_llm_callable_decodes_analysis_request() -> None:
    from ops.llm_edge_finder_sdk import make_sdk_llm_callable

    callable_ = make_sdk_llm_callable(
        client=_FakeAnthropicClient(  # type: ignore[arg-type]
            response_text='{"kind":"AnalysisRequest","rationale":"r","tool_calls":[]}',
        )
    )
    result = await callable_("sys", "user", [])
    assert result["kind"] == "AnalysisRequest"
    assert result["rationale"] == "r"


@pytest.mark.asyncio
async def test_sdk_llm_callable_json_decode_failure_returns_synthetic_request() -> None:
    """Malformed JSON from LLM → synthetic AnalysisRequest (loop continues)."""
    from ops.llm_edge_finder_sdk import make_sdk_llm_callable

    callable_ = make_sdk_llm_callable(
        client=_FakeAnthropicClient(response_text="this is not JSON"),  # type: ignore[arg-type]
    )
    result = await callable_("sys", "user", [])
    assert result["kind"] == "AnalysisRequest"
    assert "json_decode_failed" in result["rationale"]
    assert result["tool_calls"] == []


@pytest.mark.asyncio
async def test_sdk_llm_callable_authskip_on_auth_error() -> None:
    """AuthenticationError → AuthSkip (caller skips, no retry)."""
    from ops.llm_edge_finder_sdk import AuthSkip, make_sdk_llm_callable

    callable_ = make_sdk_llm_callable(client=_AuthErrorClient())  # type: ignore[arg-type]
    with pytest.raises(AuthSkip):
        await callable_("sys", "user", [])


# ───────────────────────── safety contract ─────────────────────────


@pytest.mark.asyncio
async def test_sdk_no_tools_param() -> None:
    """The SDK call MUST NOT include a `tools` param (advisory contract)."""
    from ops.llm_edge_finder_sdk import make_sdk_llm_callable

    capture: list[dict[str, Any]] = []
    callable_ = make_sdk_llm_callable(
        client=_FakeAnthropicClient(capture=capture),  # type: ignore[arg-type]
    )
    await callable_("sys", "user", [])
    assert len(capture) == 1
    assert "tools" not in capture[0]


@pytest.mark.asyncio
async def test_sdk_temperature_zero() -> None:
    """temperature=0.0 for deterministic-as-possible replies."""
    from ops.llm_edge_finder_sdk import make_sdk_llm_callable

    capture: list[dict[str, Any]] = []
    callable_ = make_sdk_llm_callable(
        client=_FakeAnthropicClient(capture=capture),  # type: ignore[arg-type]
    )
    await callable_("sys", "user", [])
    assert capture[0]["temperature"] == 0.0


@pytest.mark.asyncio
async def test_sdk_uses_system_prompt() -> None:
    """The agent's persona text goes in `system`, not `messages`."""
    from ops.llm_edge_finder_sdk import make_sdk_llm_callable

    capture: list[dict[str, Any]] = []
    callable_ = make_sdk_llm_callable(
        client=_FakeAnthropicClient(capture=capture),  # type: ignore[arg-type]
    )
    await callable_("PERSONA_HERE", "user", [])
    assert capture[0]["system"] == "PERSONA_HERE"


# ───────────────────────── transcript → messages shape ─────────────────────────


@pytest.mark.asyncio
async def test_transcript_serializes_into_messages() -> None:
    """Each transcript turn → assistant (AnalysisRequest) + user (tool_results) pair."""
    from ops.llm_edge_finder_sdk import make_sdk_llm_callable

    capture: list[dict[str, Any]] = []
    callable_ = make_sdk_llm_callable(
        client=_FakeAnthropicClient(capture=capture),  # type: ignore[arg-type]
    )
    transcript = [
        {
            "turn": 1,
            "rationale": "turn-1 analyzing",
            "tool_calls": [{"callable_name": "OLS_HAC_NW", "args_json": "{}"}],
            "tool_results": [{"call": {"callable_name": "OLS_HAC_NW", "args_json": "{}"}, "numeric_summary": {"statistic": 0.42}}],
        }
    ]
    await callable_("sys", "user", transcript)
    msgs = capture[0]["messages"]
    # First message = first-turn user prompt
    assert msgs[0]["role"] == "user"
    assert msgs[0]["content"] == "user"
    # Second = assistant's prior emission
    assert msgs[1]["role"] == "assistant"
    assert "AnalysisRequest" in msgs[1]["content"]
    # Third = user feeding back the tool results
    assert msgs[2]["role"] == "user"
    assert "tool_results" in msgs[2]["content"]


@pytest.mark.asyncio
async def test_empty_transcript_messages_first_user_only() -> None:
    from ops.llm_edge_finder_sdk import make_sdk_llm_callable

    capture: list[dict[str, Any]] = []
    callable_ = make_sdk_llm_callable(
        client=_FakeAnthropicClient(capture=capture),  # type: ignore[arg-type]
    )
    await callable_("sys", "first turn user", [])
    msgs = capture[0]["messages"]
    assert len(msgs) == 1
    assert msgs[0]["content"] == "first turn user"


# ───────────────────────── end-to-end with the agent ─────────────────────────


@pytest.mark.asyncio
async def test_sdk_integrates_with_run_finder() -> None:
    """SDK-bound callable feeds run_finder; FinderRun.proposed_spec_count is recorded."""
    from datetime import date

    from ops.llm_edge_finder import run_finder
    from ops.llm_edge_finder_sdk import make_sdk_llm_callable

    # Inline FakePool to avoid cross-file imports.
    class _FakeConn:
        async def execute(self, _sql: str, *_args: Any) -> None:
            pass
        async def fetch(self, _sql: str, *_args: Any) -> list[dict[str, Any]]:
            return []
        async def fetchrow(self, _sql: str, *_args: Any) -> dict[str, Any] | None:
            return None

    class _AcquireCM:
        async def __aenter__(self) -> _FakeConn:
            return _FakeConn()
        async def __aexit__(self, *exc: object) -> None:
            return None

    class _FakePool:
        def acquire(self) -> _AcquireCM:
            return _AcquireCM()

    llm_callable = make_sdk_llm_callable(
        client=_FakeAnthropicClient(  # type: ignore[arg-type]
            response_text='{"kind":"AnalysisResult","proposed_specs":[],"finder_rationale":"empty"}',
        ),
    )
    run = await run_finder(
        _FakePool(),  # type: ignore[arg-type]
        trigger="operator_command",
        session_date=date(2026, 5, 21),
        llm_callable=llm_callable,
    )
    assert run.trigger == "operator_command"
    assert run.proposed_spec_count == 0
