import pytest

from src.tools.telephony.check_voicemail import CheckVoicemailTool


def test_definition_forbids_acting_on_assistant_voicemail_offer():
    assert "Never invoke" in CheckVoicemailTool().definition.description


@pytest.mark.asyncio
async def test_check_voicemail_requires_explicit_latest_user_intent(
    tool_context, sample_call_session
):
    sample_call_session.conversation_history = [
        {"role": "assistant", "content": "I can check voicemail."},
        {"role": "user", "content": "Call Vada Vee Restaurant."},
    ]

    result = await CheckVoicemailTool().execute({}, tool_context)

    assert result["status"] == "error"
    assert sample_call_session.pending_deferred_transfer is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "utterance",
    ["Check my messages.", "Play my voicemail.", "Do I have any voice mail?"],
)
async def test_check_voicemail_accepts_explicit_retrieval_intent(
    utterance, tool_context, sample_call_session
):
    sample_call_session.conversation_history = [
        {"role": "user", "content": utterance}
    ]

    result = await CheckVoicemailTool().execute({}, tool_context)

    assert result["status"] == "success"
    assert sample_call_session.pending_deferred_transfer["transfer_type"] == "voicemail_main"
    assert sample_call_session.pending_deferred_transfer["armed_user_turn_count"] == 1
