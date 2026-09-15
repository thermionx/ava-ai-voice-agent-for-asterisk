from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.engine import Engine


@pytest.fixture
def engine():
    instance = Engine.__new__(Engine)
    instance._local_ai_server_llm_request = AsyncMock(return_value="NONE")
    return instance


@pytest.mark.parametrize("utterance, expected", [
    ("Hi, I'm looking for Brian", ""),
    ("I'm trying to reach Katherine.", ""),
    ("I'm calling about lunch.", ""),
    ("I'm just catching up.", ""),
    ("I'm Brian's friend.", ""),
    ("Well, yeah, my name is Brian.", "Brian"),
    ("I'm Katherine.", "Katherine"),
    ("This is Mary Jane.", "Mary Jane"),
    ("My name is Jean-Luc.", "Jean-luc"),
])
def test_explicit_identity_does_not_learn_requests(engine, utterance, expected):
    assert engine._operator_zero_extract_name_from_utterance(utterance) == expected


@pytest.mark.asyncio
async def test_successful_call_directory_uses_identity_not_request_or_reason(engine):
    session = SimpleNamespace(conversation_history=[
        {"role": "user", "content": "Hi, I'm looking for Brian"},
        {"role": "assistant", "content": "Sure, who's calling, please?"},
        {"role": "user", "content": "Bye-bye."},
        {"role": "user", "content": "Hello?"},
        {"role": "assistant", "content": "I hear you. Could you tell me your name, please?"},
        {"role": "user", "content": "Well, yeah, my name is Brian."},
        {"role": "assistant", "content": "Thank you, Brian. What are you calling about?"},
        {"role": "user", "content": "I'm just catching up with my friend Brian."},
    ])
    assert await engine._operator_zero_extract_spoken_caller_name(session) == "Brian"
    assert await engine._operator_zero_extract_spoken_business_name(session) == ""
    engine._local_ai_server_llm_request.assert_not_awaited()


@pytest.mark.parametrize("utterance, expected", [
    ("I'm just catching up with my friend Brian.", ""),
    ("I'm with my colleague Katherine.", ""),
    ("I'm calling from home.", ""),
    ("I'm Susan with Pacific Gas and Electric.", "Pacific Gas and Electric"),
    ("This is Brian from Acme Plumbing.", "Acme Plumbing"),
    ("I'm calling from Acme Plumbing.", "Acme Plumbing"),
    ("I am with Acme Plumbing.", "Acme Plumbing"),
])
@pytest.mark.asyncio
async def test_business_requires_organization_context(engine, utterance, expected):
    session = SimpleNamespace(conversation_history=[{"role": "user", "content": utterance}])
    assert await engine._operator_zero_extract_spoken_business_name(session) == expected
