"""Anthropic Sessions-API wiring tests for the LLM-AAR critic SDK.

Mirrors ops/tests/test_llm_edge_finder_sdk.py shape.
Covers:
- make_sdk_aar_callable returns an async callable
- AuthSkip raised on AuthenticationError
- JSON-decode failure → synthetic empty AARCriticResponse
- session.create resources carry the AAR memstore attach
- temperature N/A (managed agent — pinned server-side)
- session.events.send carries the user text
"""
from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.xdist_group("ops_shadow")


# ───────────────────────── Fakes ─────────────────────────


class _FakeBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _FakeEvent:
    def __init__(self, event_type: str, content: list[_FakeBlock] | None = None) -> None:
        self.type = event_type
        self.content = content or []


class _FakeEventsStream:
    def __init__(self, events: list[_FakeEvent]) -> None:
        self._events = events
        self._i = 0

    def __aiter__(self) -> _FakeEventsStream:
        return self

    async def __anext__(self) -> _FakeEvent:
        if self._i >= len(self._events):
            raise StopAsyncIteration
        ev = self._events[self._i]
        self._i += 1
        return ev


class _FakeEvents:
    def __init__(self, response_text: str, send_capture: list[dict[str, Any]] | None = None) -> None:
        self._response_text = response_text
        self.send_capture = send_capture if send_capture is not None else []

    async def send(self, **kwargs: Any) -> None:
        self.send_capture.append(kwargs)

    async def stream(self, **_kwargs: Any) -> _FakeEventsStream:
        return _FakeEventsStream([
            _FakeEvent("agent.message", [_FakeBlock(self._response_text)]),
            _FakeEvent("session.status_idle"),
        ])


class _FakeSession:
    def __init__(self, session_id: str = "sesn_fake_123") -> None:
        self.id = session_id


class _FakeSessions:
    def __init__(self, response_text: str, send_capture: list[dict[str, Any]] | None = None, create_capture: list[dict[str, Any]] | None = None) -> None:
        self.events = _FakeEvents(response_text, send_capture)
        self.create_capture = create_capture if create_capture is not None else []

    async def create(self, **kwargs: Any) -> _FakeSession:
        self.create_capture.append(kwargs)
        return _FakeSession()


class _FakeBeta:
    def __init__(self, response_text: str, send_capture: list[dict[str, Any]] | None = None, create_capture: list[dict[str, Any]] | None = None) -> None:
        self.sessions = _FakeSessions(response_text, send_capture, create_capture)


class _FakeAnthropicClient:
    def __init__(
        self,
        response_text: str = '{"kind":"AARCriticResponse","findings":[],"rationale":"none"}',
        send_capture: list[dict[str, Any]] | None = None,
        create_capture: list[dict[str, Any]] | None = None,
    ) -> None:
        self.beta = _FakeBeta(response_text, send_capture, create_capture)


class _AuthErrorEvents:
    async def send(self, **_kwargs: Any) -> None:
        import httpx
        from anthropic import AuthenticationError
        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        response = httpx.Response(401, request=request)
        raise AuthenticationError(
            message="invalid api key",
            response=response,
            body={"error": "invalid_api_key"},
        )

    async def stream(self, **_kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("stream should not be called on auth error")


class _AuthErrorSessions:
    def __init__(self) -> None:
        self.events = _AuthErrorEvents()

    async def create(self, **_kwargs: Any) -> _FakeSession:
        return _FakeSession()


class _AuthErrorBeta:
    def __init__(self) -> None:
        self.sessions = _AuthErrorSessions()


class _AuthErrorClient:
    def __init__(self) -> None:
        self.beta = _AuthErrorBeta()


# ───────────────────────── make_sdk_aar_callable ─────────────────────────


@pytest.mark.asyncio
async def test_make_sdk_aar_callable_returns_async_callable() -> None:
    from ops.llm_aar_critic_sdk import make_sdk_aar_callable
    c = make_sdk_aar_callable(client=_FakeAnthropicClient())  # type: ignore[arg-type]
    assert callable(c)


@pytest.mark.asyncio
async def test_sdk_aar_callable_returns_decoded_json_envelope() -> None:
    from ops.llm_aar_critic_sdk import make_sdk_aar_callable
    c = make_sdk_aar_callable(
        client=_FakeAnthropicClient(  # type: ignore[arg-type]
            response_text='{"kind":"AARCriticResponse","findings":[],"rationale":"ok"}',
        )
    )
    result = await c("sys", "user", [])
    assert result == {"kind": "AARCriticResponse", "findings": [], "rationale": "ok"}


@pytest.mark.asyncio
async def test_sdk_aar_callable_strips_markdown_fences() -> None:
    from ops.llm_aar_critic_sdk import make_sdk_aar_callable
    c = make_sdk_aar_callable(
        client=_FakeAnthropicClient(  # type: ignore[arg-type]
            response_text='```json\n{"kind":"AARCriticResponse","findings":[],"rationale":"r"}\n```',
        )
    )
    result = await c("sys", "user", [])
    assert result["kind"] == "AARCriticResponse"


@pytest.mark.asyncio
async def test_sdk_aar_callable_json_decode_failure_returns_synthetic() -> None:
    from ops.llm_aar_critic_sdk import make_sdk_aar_callable
    c = make_sdk_aar_callable(
        client=_FakeAnthropicClient(response_text="not json"),  # type: ignore[arg-type]
    )
    result = await c("sys", "user", [])
    # Synthetic empty critic response
    assert result["kind"] == "AARCriticResponse"
    assert result["findings"] == []
    assert "json_decode_failure" in result["rationale"]


@pytest.mark.asyncio
async def test_sdk_aar_callable_authskip_on_auth_error() -> None:
    from ops.llm_aar_critic_sdk import AuthSkip, make_sdk_aar_callable
    c = make_sdk_aar_callable(client=_AuthErrorClient())  # type: ignore[arg-type]
    with pytest.raises(AuthSkip):
        await c("sys", "user", [])


# ───────────────────────── Safety contract ─────────────────────────


@pytest.mark.asyncio
async def test_sdk_aar_session_create_attaches_memstore() -> None:
    """The session.create call MUST include the AAR memstore as a resource."""
    from ops.llm_aar_critic_sdk import make_sdk_aar_callable

    create_capture: list[dict[str, Any]] = []
    c = make_sdk_aar_callable(
        client=_FakeAnthropicClient(create_capture=create_capture),  # type: ignore[arg-type]
        memstore_id="memstore_TEST_MEMSTORE_ID",
    )
    await c("sys", "user", [])
    assert len(create_capture) == 1
    resources = create_capture[0].get("resources", [])
    assert len(resources) == 1
    assert resources[0]["type"] == "memory_store"
    assert resources[0]["memory_store_id"] == "memstore_TEST_MEMSTORE_ID"
    assert resources[0]["access"] == "read_write"


@pytest.mark.asyncio
async def test_sdk_aar_send_carries_user_text() -> None:
    """The events.send call MUST carry the application's user prompt."""
    from ops.llm_aar_critic_sdk import make_sdk_aar_callable

    send_capture: list[dict[str, Any]] = []
    c = make_sdk_aar_callable(
        client=_FakeAnthropicClient(send_capture=send_capture),  # type: ignore[arg-type]
    )
    await c("sys", "PAYLOAD_USER_PROMPT_HERE", [])
    assert len(send_capture) == 1
    events = send_capture[0]["events"]
    assert events[0]["type"] == "user.message"
    assert events[0]["content"][0]["text"] == "PAYLOAD_USER_PROMPT_HERE"


@pytest.mark.asyncio
async def test_sdk_aar_uses_managed_agents_beta() -> None:
    """Both session.create + events.send MUST carry the managed-agents beta header."""
    from ops.llm_aar_anthropic_ids import MANAGED_AGENTS_BETA
    from ops.llm_aar_critic_sdk import make_sdk_aar_callable

    create_capture: list[dict[str, Any]] = []
    send_capture: list[dict[str, Any]] = []
    c = make_sdk_aar_callable(
        client=_FakeAnthropicClient(  # type: ignore[arg-type]
            send_capture=send_capture,
            create_capture=create_capture,
        ),
    )
    await c("sys", "user", [])
    assert create_capture[0]["betas"] == [MANAGED_AGENTS_BETA]
    assert send_capture[0]["betas"] == [MANAGED_AGENTS_BETA]


@pytest.mark.asyncio
async def test_sdk_aar_persona_sha_mismatch_warns_only() -> None:
    """Persona SHA mismatch logs a warning; does NOT raise (mirrors finder SDK)."""
    # Bypass the PENDING_PROVISION early return by patching the module
    # constant for this test only.
    import ops.llm_aar_critic_sdk as sdk_mod
    from ops.llm_aar_critic_sdk import make_sdk_aar_callable
    original = sdk_mod.PROVISIONED_PERSONA_SHA256
    sdk_mod.PROVISIONED_PERSONA_SHA256 = "deadbeef" * 8
    try:
        c = make_sdk_aar_callable(client=_FakeAnthropicClient())  # type: ignore[arg-type]
        # Does not raise; persona_sha mismatch is a soft warn.
        result = await c("NOT_THE_PROVISIONED_PERSONA", "user", [])
        assert result["kind"] == "AARCriticResponse"
    finally:
        sdk_mod.PROVISIONED_PERSONA_SHA256 = original
