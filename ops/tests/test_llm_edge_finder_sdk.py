"""Anthropic Sessions API wiring tests for the LLM edge-finder — Task #25 + 2026-05-22.

Covers the post-Sessions-API SDK shape (``ops/llm_edge_finder_sdk.py``):
- ``make_sdk_llm_callable`` returns an async callable that holds a session
  across multi-turn application loops.
- First call opens a session via ``client.beta.sessions.create`` with the
  finder agent + environment + memstore attached.
- Subsequent calls reuse the open session and only send the latest
  ``tool_results`` as the next ``user.message``.
- ``AuthSkip`` is raised on ``AuthenticationError`` (no API key path).
- JSON-decode failure → synthetic ``AnalysisRequest`` (loop continues).
- ``session.error`` event raises ``AgentSessionError`` (fail-loud).
- The application's tool sandbox is NOT registered on the agent (custom
  ``tools=[]`` would violate persona §2.8; the agent uses
  ``agent_toolset_20260401`` for memstore file ops only).
- Persona-SHA defense check logs a warning on drift (does not raise).
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

pytestmark = pytest.mark.xdist_group("ops_shadow")


# ───────────────────────── Fakes (Sessions API surface) ─────────────────────────


class _FakeSession:
    def __init__(self, session_id: str) -> None:
        self.id = session_id


class _FakeTextBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _FakeAgentMessageEvent:
    def __init__(self, text: str) -> None:
        self.type = "agent.message"
        self.content = [_FakeTextBlock(text)]


class _FakeAgentToolUseEvent:
    """A file-tool use (read/glob/write/...) the agent fires against the memstore."""
    def __init__(self, tool: str, path: str = "") -> None:
        self.type = "agent.tool_use"
        self.tool = tool
        self.path = path


class _FakeAgentToolResultEvent:
    def __init__(self, content: str) -> None:
        self.type = "agent.tool_result"
        self.content = content


class _FakeStopReason:
    def __init__(self, reason: str = "end_turn") -> None:
        self.type = reason


class _FakeIdleEvent:
    def __init__(self, stop_reason: str = "end_turn") -> None:
        self.type = "session.status_idle"
        self.stop_reason = _FakeStopReason(stop_reason)


class _FakeSessionErrorEvent:
    def __init__(self, msg: str) -> None:
        self.type = "session.error"
        self.error = msg


class _FakeAsyncStream:
    """Async iterator over a fixed event list."""
    def __init__(self, events: list[Any]) -> None:
        self._events = list(events)

    def __aiter__(self) -> _FakeAsyncStream:
        return self

    async def __anext__(self) -> Any:
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)


class _FakeSessionsService:
    """Captures `sessions.create` / `events.send` / `events.stream` calls."""
    def __init__(
        self,
        agent_responses: list[str],
        create_capture: list[dict[str, Any]] | None = None,
        send_capture: list[dict[str, Any]] | None = None,
        stream_events_factory: Any | None = None,
    ) -> None:
        self._agent_responses = list(agent_responses)
        self._create_capture = create_capture if create_capture is not None else []
        self._send_capture = send_capture if send_capture is not None else []
        self._stream_events_factory = stream_events_factory
        self._session_counter = 0
        self.events = self  # so `client.beta.sessions.events.send` works on us

    async def create(self, **kwargs: Any) -> _FakeSession:
        self._create_capture.append(kwargs)
        self._session_counter += 1
        return _FakeSession(f"sesn_test_{self._session_counter:03d}")

    async def send(self, session_id: str, **kwargs: Any) -> dict[str, Any]:
        self._send_capture.append({"session_id": session_id, **kwargs})
        return {}

    async def stream(self, session_id: str, **kwargs: Any) -> _FakeAsyncStream:
        del kwargs
        if self._stream_events_factory is not None:
            events = self._stream_events_factory(session_id)
        elif self._agent_responses:
            text = self._agent_responses.pop(0)
            events = [_FakeAgentMessageEvent(text), _FakeIdleEvent()]
        else:
            events = [_FakeIdleEvent()]
        return _FakeAsyncStream(events)


class _FakeBetaWithSessions:
    def __init__(self, sessions: _FakeSessionsService) -> None:
        self.sessions = sessions


class _FakeAnthropicClient:
    def __init__(
        self,
        response_text: str = '{"kind":"AnalysisResult","proposed_specs":[],"finder_rationale":"x"}',
        send_capture: list[dict[str, Any]] | None = None,
        create_capture: list[dict[str, Any]] | None = None,
    ) -> None:
        sessions = _FakeSessionsService(
            agent_responses=[response_text],
            create_capture=create_capture,
            send_capture=send_capture,
        )
        self.beta = _FakeBetaWithSessions(sessions)
        self.sessions = sessions


class _AuthErrorSessionsService:
    def __init__(self) -> None:
        self.events = self

    async def create(self, **kwargs: Any) -> Any:
        del kwargs
        import httpx
        from anthropic import AuthenticationError
        request = httpx.Request("POST", "https://api.anthropic.com/v1/sessions")
        response = httpx.Response(401, request=request)
        raise AuthenticationError(
            message="invalid api key",
            response=response,
            body={"error": "invalid_api_key"},
        )


class _AuthErrorClient:
    def __init__(self) -> None:
        self.beta = _FakeBetaWithSessions(_AuthErrorSessionsService())


# ───────────────────────── make_sdk_llm_callable ─────────────────────────


@pytest.mark.asyncio
async def test_make_sdk_llm_callable_returns_async_callable() -> None:
    from ops.llm_edge_finder_sdk import make_sdk_llm_callable

    callable_ = make_sdk_llm_callable(client=_FakeAnthropicClient())  # type: ignore[arg-type]
    assert callable(callable_)


@pytest.mark.asyncio
async def test_sdk_first_call_creates_session_with_memstore_attached() -> None:
    """First _call → sessions.create with agent_id + env_id + memstore resource."""
    from ops.llm_edge_finder_sdk import make_sdk_llm_callable
    from ops.llm_finder_anthropic_ids import (
        EDGE_FINDER_AGENT_ID,
        EDGE_FINDER_ENVIRONMENT_ID,
        EDGE_FINDER_MEMSTORE_ID,
    )

    create_capture: list[dict[str, Any]] = []
    client = _FakeAnthropicClient(
        response_text='{"kind":"AnalysisResult","proposed_specs":[],"finder_rationale":"ok"}',
        create_capture=create_capture,
    )
    callable_ = make_sdk_llm_callable(client=client)  # type: ignore[arg-type]
    await callable_("sys", "first-turn user prompt", [])
    assert len(create_capture) == 1
    create_kwargs = create_capture[0]
    assert create_kwargs["agent"] == EDGE_FINDER_AGENT_ID
    assert create_kwargs["environment_id"] == EDGE_FINDER_ENVIRONMENT_ID
    # Memstore resource attached as read_write
    resources = list(create_kwargs["resources"])
    assert len(resources) == 1
    assert resources[0]["type"] == "memory_store"
    assert resources[0]["memory_store_id"] == EDGE_FINDER_MEMSTORE_ID
    assert resources[0]["access"] == "read_write"
    # Managed-agents beta header passed
    assert "managed-agents-2026-04-01" in create_kwargs["betas"]


@pytest.mark.asyncio
async def test_sdk_first_call_sends_user_prompt_as_first_user_message() -> None:
    """First _call → events.send with the first-turn user prompt verbatim."""
    from ops.llm_edge_finder_sdk import make_sdk_llm_callable

    send_capture: list[dict[str, Any]] = []
    client = _FakeAnthropicClient(
        response_text='{"kind":"AnalysisResult","proposed_specs":[],"finder_rationale":"x"}',
        send_capture=send_capture,
    )
    callable_ = make_sdk_llm_callable(client=client)  # type: ignore[arg-type]
    await callable_("sys", "FIRST_TURN_USER_PROMPT", [])
    assert len(send_capture) == 1
    events = list(send_capture[0]["events"])
    assert events[0]["type"] == "user.message"
    text_block = list(events[0]["content"])[0]
    assert text_block["type"] == "text"
    assert text_block["text"] == "FIRST_TURN_USER_PROMPT"


@pytest.mark.asyncio
async def test_sdk_returns_decoded_json_envelope_from_agent_message() -> None:
    """Agent's text body is parsed as JSON and returned to the caller."""
    from ops.llm_edge_finder_sdk import make_sdk_llm_callable

    callable_ = make_sdk_llm_callable(
        client=_FakeAnthropicClient(  # type: ignore[arg-type]
            response_text='{"kind":"AnalysisResult","proposed_specs":[],"finder_rationale":"ok"}',
        )
    )
    result = await callable_("sys", "user", [])
    assert result == {"kind": "AnalysisResult", "proposed_specs": [], "finder_rationale": "ok"}


@pytest.mark.asyncio
async def test_sdk_decodes_analysis_request_kind() -> None:
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
async def test_sdk_subsequent_call_reuses_session_and_sends_tool_results() -> None:
    """Second _call → no new sessions.create; events.send with the latest tool_results."""
    from ops.llm_edge_finder_sdk import make_sdk_llm_callable

    create_capture: list[dict[str, Any]] = []
    send_capture: list[dict[str, Any]] = []
    # Two response texts to handle the two turns
    sessions = _FakeSessionsService(
        agent_responses=[
            '{"kind":"AnalysisRequest","rationale":"r1","tool_calls":[]}',
            '{"kind":"AnalysisResult","proposed_specs":[],"finder_rationale":"done"}',
        ],
        create_capture=create_capture,
        send_capture=send_capture,
    )

    class _Client:
        def __init__(self) -> None:
            self.beta = _FakeBetaWithSessions(sessions)

    callable_ = make_sdk_llm_callable(client=_Client())  # type: ignore[arg-type]
    # Turn 1 — empty transcript
    out1 = await callable_("sys", "first turn", [])
    assert out1["kind"] == "AnalysisRequest"
    # Turn 2 — application appended one transcript entry with tool_results
    transcript = [
        {
            "turn": 1,
            "rationale": "r1",
            "tool_calls": [{"callable_name": "OLS_HAC_NW", "args_json": "{}"}],
            "tool_results": [
                {"call": {"callable_name": "OLS_HAC_NW", "args_json": "{}"},
                 "numeric_summary": {"statistic": 0.42}}
            ],
        }
    ]
    out2 = await callable_("sys", "first turn", transcript)
    assert out2["kind"] == "AnalysisResult"
    # Exactly ONE session created across both turns
    assert len(create_capture) == 1
    # Two send-events calls (one per turn)
    assert len(send_capture) == 2
    # Second send carried the tool_results, NOT the first-turn user prompt
    second_event = list(send_capture[1]["events"])[0]
    body = json.loads(list(second_event["content"])[0]["text"])
    assert "tool_results" in body
    assert body["tool_results"][0]["numeric_summary"]["statistic"] == 0.42


@pytest.mark.asyncio
async def test_sdk_json_decode_failure_returns_synthetic_request() -> None:
    """Malformed JSON from LLM → synthetic AnalysisRequest (loop continues)."""
    from ops.llm_edge_finder_sdk import make_sdk_llm_callable

    callable_ = make_sdk_llm_callable(
        client=_FakeAnthropicClient(response_text="this is not JSON"),  # type: ignore[arg-type]
    )
    result = await callable_("sys", "user", [])
    assert result["kind"] == "AnalysisRequest"
    assert "JSON" in result["rationale"]
    assert result["tool_calls"] == []


@pytest.mark.asyncio
async def test_sdk_authskip_on_auth_error() -> None:
    """AuthenticationError on sessions.create → AuthSkip (caller skips, no retry)."""
    from ops.llm_edge_finder_sdk import AuthSkip, make_sdk_llm_callable

    callable_ = make_sdk_llm_callable(client=_AuthErrorClient())  # type: ignore[arg-type]
    with pytest.raises(AuthSkip):
        await callable_("sys", "user", [])


@pytest.mark.asyncio
async def test_sdk_session_error_event_raises_agentsessionerror() -> None:
    """A session.error event in the stream raises AgentSessionError (fail loud)."""
    from ops.llm_edge_finder_sdk import AgentSessionError, make_sdk_llm_callable

    def _error_stream(_session_id: str) -> list[Any]:
        return [_FakeSessionErrorEvent("simulated platform failure")]

    sessions = _FakeSessionsService(agent_responses=[], stream_events_factory=_error_stream)

    class _Client:
        def __init__(self) -> None:
            self.beta = _FakeBetaWithSessions(sessions)

    callable_ = make_sdk_llm_callable(client=_Client())  # type: ignore[arg-type]
    with pytest.raises(AgentSessionError):
        await callable_("sys", "user", [])


@pytest.mark.asyncio
async def test_sdk_tool_use_events_ignored_during_event_consumption() -> None:
    """The agent's file-tool use (read/glob/write against the memstore) is informational —
    it must NOT interfere with extracting the final agent.message JSON envelope."""
    from ops.llm_edge_finder_sdk import make_sdk_llm_callable

    def _stream_with_tools(_session_id: str) -> list[Any]:
        return [
            _FakeAgentToolUseEvent(tool="read", path="/mnt/memory/lab-finder/prior-emissions/x.md"),
            _FakeAgentToolResultEvent("(prior emission body)"),
            _FakeAgentToolUseEvent(tool="glob", path="/mnt/memory/lab-finder/outcomes/"),
            _FakeAgentToolResultEvent("[]"),
            _FakeAgentMessageEvent('{"kind":"AnalysisResult","proposed_specs":[],"finder_rationale":"after memstore reads"}'),
            _FakeIdleEvent(),
        ]

    sessions = _FakeSessionsService(agent_responses=[], stream_events_factory=_stream_with_tools)

    class _Client:
        def __init__(self) -> None:
            self.beta = _FakeBetaWithSessions(sessions)

    callable_ = make_sdk_llm_callable(client=_Client())  # type: ignore[arg-type]
    result = await callable_("sys", "user", [])
    assert result == {"kind": "AnalysisResult", "proposed_specs": [], "finder_rationale": "after memstore reads"}


# ───────────────────────── safety contract ─────────────────────────


@pytest.mark.asyncio
async def test_sdk_session_creation_does_not_register_custom_tools() -> None:
    """The persona §2.8 fence: custom application tools (OLS_HAC_NW etc.) are
    NEVER registered on the agent. They flow as JSON envelopes the application
    parses + runs locally. The session resources contain ONLY the memstore."""
    from ops.llm_edge_finder_sdk import make_sdk_llm_callable

    create_capture: list[dict[str, Any]] = []
    callable_ = make_sdk_llm_callable(
        client=_FakeAnthropicClient(create_capture=create_capture),  # type: ignore[arg-type]
    )
    await callable_("sys", "user", [])
    assert len(create_capture) == 1
    create_kwargs = create_capture[0]
    # Resources is memstore-only; no custom tool resources
    resources = list(create_kwargs["resources"])
    assert all(r["type"] == "memory_store" for r in resources)
    # No `tools` field on session create (tools live on the Agent, not the Session)
    assert "tools" not in create_kwargs


@pytest.mark.asyncio
async def test_sdk_session_uses_managed_agents_beta_header() -> None:
    from ops.llm_edge_finder_sdk import make_sdk_llm_callable

    create_capture: list[dict[str, Any]] = []
    send_capture: list[dict[str, Any]] = []
    client = _FakeAnthropicClient(
        create_capture=create_capture,
        send_capture=send_capture,
    )
    callable_ = make_sdk_llm_callable(client=client)  # type: ignore[arg-type]
    await callable_("sys", "user", [])
    assert "managed-agents-2026-04-01" in create_capture[0]["betas"]
    assert "managed-agents-2026-04-01" in send_capture[0]["betas"]


@pytest.mark.asyncio
async def test_sdk_persona_mismatch_logs_warning_but_does_not_raise() -> None:
    """An in-process system_prompt that differs from the provisioned SHA logs a
    warning but does NOT abort the run. The Anthropic agent's server-side
    persona is authoritative; the in-process arg is defense-in-depth only."""
    from ops.llm_edge_finder_sdk import make_sdk_llm_callable

    callable_ = make_sdk_llm_callable(client=_FakeAnthropicClient())  # type: ignore[arg-type]
    # Pass a deliberately-different system prompt — should not raise.
    result = await callable_("DRIFT_PERSONA_TEXT", "user", [])
    assert "kind" in result


# ───────────────────────── end-to-end with the agent ─────────────────────────


@pytest.mark.asyncio
async def test_sdk_integrates_with_run_finder() -> None:
    """SDK-bound callable feeds run_finder; FinderRun.proposed_spec_count is recorded."""
    from datetime import date

    from ops.llm_edge_finder import run_finder
    from ops.llm_edge_finder_sdk import make_sdk_llm_callable

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


# ───────────────────────── archive_emission_to_memstore ─────────────────────────


class _FakeMemory:
    def __init__(self, memory_id: str = "mem_test_001") -> None:
        self.id = memory_id


class _FakeMemoriesService:
    def __init__(self, capture: list[dict[str, Any]] | None = None,
                 raise_exc: Exception | None = None) -> None:
        self._capture = capture if capture is not None else []
        self._raise = raise_exc

    async def create(self, **kwargs: Any) -> _FakeMemory:
        self._capture.append(kwargs)
        if self._raise is not None:
            raise self._raise
        return _FakeMemory()


class _FakeMemoryStoresWithMemories:
    def __init__(self, memories: _FakeMemoriesService) -> None:
        self.memories = memories


class _FakeBetaWithMemoryStores:
    def __init__(self, sessions: Any, memory_stores: _FakeMemoryStoresWithMemories) -> None:
        self.sessions = sessions
        self.memory_stores = memory_stores


@pytest.mark.asyncio
async def test_archive_emission_to_memstore_writes_prior_emissions_path() -> None:
    """archive_emission_to_memstore writes /prior-emissions/<candidate>.md."""
    from ops.llm_edge_finder_sdk import archive_emission_to_memstore

    capture: list[dict[str, Any]] = []
    memories = _FakeMemoriesService(capture=capture)

    class _Client:
        def __init__(self) -> None:
            self.beta = _FakeBetaWithMemoryStores(
                sessions=None,
                memory_stores=_FakeMemoryStoresWithMemories(memories),
            )

    spec_dict = {
        "candidate_name": "test_cand_xyz",
        "target_engine": "catalyst",
        "intent": "fold_existing",
        "primary_hypothesis": "test hypothesis text",
        "rationale": "test rationale",
        "regime_tuple_id": "abc123",
        "cost_assumption_bps_roundtrip": 8,
        "falsification_criterion": "test falsification",
    }
    memory_id = await archive_emission_to_memstore(
        spec_dict, run_id="test-run-id-001", session_date="2026-05-22",
        client=_Client(),  # type: ignore[arg-type]
    )
    assert memory_id == "mem_test_001"
    assert len(capture) == 1
    assert capture[0]["path"] == "/prior-emissions/test_cand_xyz.md"
    content = capture[0]["content"]
    assert "# test_cand_xyz" in content
    assert "test hypothesis text" in content
    assert "test rationale" in content
    assert "test falsification" in content


@pytest.mark.asyncio
async def test_archive_emission_to_memstore_warns_on_failure() -> None:
    """Memstore-write failure is logged + None-returned (NEVER raises)."""
    from ops.llm_edge_finder_sdk import archive_emission_to_memstore

    memories = _FakeMemoriesService(raise_exc=RuntimeError("simulated failure"))

    class _Client:
        def __init__(self) -> None:
            self.beta = _FakeBetaWithMemoryStores(
                sessions=None,
                memory_stores=_FakeMemoryStoresWithMemories(memories),
            )

    memory_id = await archive_emission_to_memstore(
        {"candidate_name": "x", "target_engine": "y"},
        run_id="rid", session_date="2026-05-22",
        client=_Client(),  # type: ignore[arg-type]
    )
    assert memory_id is None


if TYPE_CHECKING:  # pragma: no cover
    pass
