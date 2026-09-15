"""Protect greeting generation at the provider, as well as local playback."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.config import OpenAIRealtimeProviderConfig
from src.providers.openai_realtime import OpenAIRealtimeProvider


def provider_for(*, interruptible=False, greeting="Hello. How can I help you?", api_version="ga"):
    config = OpenAIRealtimeProviderConfig(
        api_key="test-key", model="gpt-realtime", api_version=api_version,
        greeting=greeting, greeting_interruptible=interruptible,
        turn_detection={"type": "server_vad", "threshold": 0.7,
                        "silence_duration_ms": 800, "prefix_padding_ms": 400},
    )
    provider = OpenAIRealtimeProvider(config, on_event=AsyncMock())
    provider._call_id = "greeting-regression"
    provider.websocket = SimpleNamespace(state=SimpleNamespace(name="OPEN"))
    provider._send_json = AsyncMock()
    return provider


@pytest.mark.asyncio
@pytest.mark.parametrize("interruptible,greeting,expected", [
    (False, "Hello", False), (True, "Hello", True), (False, "", True),
])
async def test_initial_ga_session_protects_only_noninterruptible_greeting(interruptible, greeting, expected):
    provider = provider_for(interruptible=interruptible, greeting=greeting)
    await provider._send_session_update()
    td = provider._send_json.await_args.args[0]["session"]["audio"]["input"]["turn_detection"]
    assert td["interrupt_response"] is expected
    assert td["create_response"] is True
    assert td["threshold"] == 0.7


@pytest.mark.asyncio
@pytest.mark.parametrize("interruptible", [False, True])
async def test_ga_greeting_sets_server_policy_before_response(interruptible):
    provider = provider_for(interruptible=interruptible)
    try:
        await provider._send_explicit_greeting()
        messages = [call.args[0] for call in provider._send_json.await_args_list]
        assert messages[0]["type"] == "session.update"
        session = messages[0]["session"]
        assert session["type"] == "realtime"
        assert "turn_detection" not in session
        td = session["audio"]["input"]["turn_detection"]
        assert td["type"] == "server_vad"
        assert td["interrupt_response"] is interruptible
        assert td["create_response"] is True
        assert td["threshold"] == 0.7
        assert td["silence_duration_ms"] == 800
        assert td["prefix_padding_ms"] == 400
        assert messages[1]["type"] == "response.create"
        assert provider._greeting_transport_guard_active is not interruptible
    finally:
        if provider._greeting_vad_task:
            provider._greeting_vad_task.cancel()
            await asyncio.gather(provider._greeting_vad_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_ga_generation_completion_restores_vad_but_keeps_transport_guard():
    provider = provider_for()
    provider._greeting_transport_guard_active = True
    provider._greeting_response_id = provider._current_response_id = "greeting-response"
    provider._audio_seen_response_ids.add("greeting-response")
    provider._cancel_response = AsyncMock()
    await provider._handle_event({"type": "input_audio_buffer.speech_started"})
    provider._cancel_response.assert_not_awaited()
    await provider._handle_event({"type": "response.done", "response": {
        "id": "greeting-response", "status": "completed", "output": [],
    }})
    td = provider._send_json.await_args.args[0]["session"]["audio"]["input"]["turn_detection"]
    assert td == {"type": "server_vad", "threshold": 0.7, "silence_duration_ms": 800,
                  "prefix_padding_ms": 400, "create_response": True, "interrupt_response": True}
    assert provider._greeting_completed is True
    assert provider._greeting_transport_guard_active is True
    provider.on_event.reset_mock()
    await provider._handle_event({"type": "input_audio_buffer.speech_started"})
    provider.on_event.assert_not_awaited()
    await provider.release_greeting_transport_guard()
    assert provider._greeting_transport_guard_active is False


@pytest.mark.asyncio
async def test_ga_timeout_restores_provider_interruptions(monkeypatch):
    provider = provider_for()
    provider._greeting_transport_guard_active = True
    monkeypatch.setattr("src.providers.openai_realtime.asyncio.sleep", AsyncMock())
    await provider._greeting_vad_fallback()
    messages = [call.args[0] for call in provider._send_json.await_args_list]
    assert messages[0]["session"]["audio"]["input"]["turn_detection"]["interrupt_response"] is True
    assert messages[1]["type"] == "input_audio_buffer.clear"
    assert provider._greeting_transport_guard_active is False


@pytest.mark.asyncio
async def test_beta_vad_restore_keeps_flat_schema():
    provider = provider_for(api_version="beta")
    await provider._re_enable_vad()
    session = provider._send_json.await_args.args[0]["session"]
    assert "audio" not in session
    assert session["turn_detection"]["threshold"] == 0.7


@pytest.mark.asyncio
async def test_closed_session_does_not_attempt_vad_restore():
    provider = provider_for()
    provider.websocket.state.name = "CLOSED"
    await provider._re_enable_vad()
    provider._send_json.assert_not_awaited()
