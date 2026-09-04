import json

import pytest

from src.tools.adapters.openai import OpenAIToolAdapter


class _WebSocket:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(json.loads(payload))


@pytest.mark.asyncio
async def test_web_search_result_uses_concise_directory_assistance_instruction():
    adapter = OpenAIToolAdapter(registry=None)
    websocket = _WebSocket()
    result = {
        "call_id": "tool-call-1",
        "function_name": "web_search",
        "status": "success",
        "message": "A deliberately long search result that must not be repeated verbatim.",
    }

    await adapter.send_tool_result(
        result,
        {"websocket": websocket, "call_id": "call-1", "is_ga": True},
    )

    assert websocket.sent[0]["type"] == "conversation.item.create"
    response = websocket.sent[1]
    assert response["type"] == "response.create"
    instructions = response["response"]["instructions"]
    assert "at most two short sentences" in instructions
    assert "deliberately long search result" not in instructions
