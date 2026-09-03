import time
from unittest.mock import AsyncMock

import pytest

from src.tools.telephony.deferred_transfer import DEFERRED_TRANSFER_RESULT_KEY
from src.tools.telephony.dial_phone import DialPhoneTool, normalize_nanp_number


@pytest.fixture
def tool():
    return DialPhoneTool()


def test_normalize_nanp_number_accepts_common_formats():
    assert normalize_nanp_number("(925) 555-0123") == "9255550123"
    assert normalize_nanp_number("+1 925 555 0123") == "19255550123"


@pytest.mark.parametrize(
    "value",
    ["", "911", "1234567890", "9251550123", "011442071838750", "925-FLOWERS"],
)
def test_normalize_nanp_number_rejects_non_dialplan_targets(value):
    assert normalize_nanp_number(value) is None


@pytest.mark.asyncio
async def test_first_invocation_only_stages_and_reads_back(tool, tool_context, sample_call_session):
    result = await tool.execute(
        {"phone_number": "(925) 555-0123", "confirmed": False, "display_name": "Pat"},
        tool_context,
    )

    assert result == {
        "status": "confirmation_required",
        "phone_number": "9255550123",
        "spoken_digits": "9 2 5 5 5 5 0 1 2 3",
        "message": "Should I dial 9 2 5 5 5 5 0 1 2 3?",
    }
    assert sample_call_session.pending_phone_call["phone_number"] == "9255550123"
    assert sample_call_session.pending_deferred_transfer is None
    tool_context.ari_client.continue_in_dialplan.assert_not_awaited()


@pytest.mark.asyncio
async def test_first_invocation_marked_confirmed_still_only_stages(tool, tool_context, sample_call_session):
    result = await tool.execute(
        {"phone_number": "9255550123", "confirmed": True},
        tool_context,
    )

    assert result["status"] == "confirmation_required"
    assert sample_call_session.pending_deferred_transfer is None


@pytest.mark.asyncio
async def test_immediate_duplicate_requires_confirmation_delay(tool, tool_context, sample_call_session):
    tool_context.config["tools"]["dial_phone"] = {
        "confirmation_minimum_seconds": 5
    }
    await tool.execute({"phone_number": "9255550123", "confirmed": False}, tool_context)

    no_new_turn = await tool.execute(
        {"phone_number": "9255550123", "confirmed": True}, tool_context
    )
    assert no_new_turn["status"] == "confirmation_required"

    assert sample_call_session.pending_deferred_transfer is None


@pytest.mark.asyncio
async def test_second_confirmed_invocation_handles_late_realtime_transcript(
    tool, tool_context, sample_call_session
):
    await tool.execute({"phone_number": "9255550123", "confirmed": False}, tool_context)
    sample_call_session.pending_phone_call["created_at"] = time.time() - 1

    result = await tool.execute(
        {"phone_number": "9255550123", "confirmed": True}, tool_context
    )

    assert result["status"] == "success"
    assert sample_call_session.pending_deferred_transfer["target"] == "9255550123"


@pytest.mark.asyncio
async def test_confirmed_same_number_arms_deferred_dialplan_handoff(
    tool, tool_context, sample_call_session
):
    tool_context.config["tools"]["dial_phone"] = {"dialplan_context": "from-house"}
    await tool.execute(
        {"phone_number": "9255550123", "confirmed": False, "display_name": "Pat"},
        tool_context,
    )
    sample_call_session.conversation_history.extend(
        [
            {"role": "assistant", "content": "Is that correct?"},
            {"role": "user", "content": "Yes, that's correct."},
        ]
    )

    result = await tool.execute(
        {"phone_number": "9255550123", "confirmed": True, "display_name": "Pat"},
        tool_context,
    )

    assert result["status"] == "success"
    assert result["message"] == "Calling Pat now."
    action = result[DEFERRED_TRANSFER_RESULT_KEY]
    assert action["target"] == "9255550123"
    assert action["dialplan_context"] == "from-house"
    assert sample_call_session.pending_phone_call is None
    assert sample_call_session.pending_deferred_transfer == action
    tool_context.ari_client.continue_in_dialplan.assert_not_awaited()


@pytest.mark.asyncio
async def test_changed_number_requires_fresh_confirmation(tool, tool_context, sample_call_session):
    await tool.execute({"phone_number": "9255550123", "confirmed": False}, tool_context)
    sample_call_session.conversation_history.append({"role": "user", "content": "Yes"})

    result = await tool.execute(
        {"phone_number": "4155550123", "confirmed": True}, tool_context
    )

    assert result["status"] == "confirmation_required"
    assert sample_call_session.pending_phone_call["phone_number"] == "4155550123"
    assert sample_call_session.pending_deferred_transfer is None


@pytest.mark.asyncio
async def test_expired_confirmation_fails_closed(tool, tool_context, sample_call_session):
    tool_context.config["tools"]["dial_phone"] = {"confirmation_timeout_seconds": 10}
    await tool.execute({"phone_number": "9255550123", "confirmed": False}, tool_context)
    sample_call_session.pending_phone_call["created_at"] = time.time() - 11
    sample_call_session.conversation_history.append({"role": "user", "content": "Yes"})

    result = await tool.execute(
        {"phone_number": "9255550123", "confirmed": True}, tool_context
    )

    assert result["status"] == "error"
    assert sample_call_session.pending_phone_call is None


@pytest.mark.asyncio
async def test_commit_revalidates_target_and_continues_in_dialplan(tool, tool_context):
    action = {"target": "9255550123", "dialplan_context": "from-house"}

    result = await tool.commit_deferred_action(action, tool_context)

    assert result["status"] == "success"
    tool_context.ari_client.continue_in_dialplan.assert_awaited_once_with(
        tool_context.caller_channel_id,
        context="from-house",
        extension="9255550123",
        priority=1,
    )


@pytest.mark.asyncio
async def test_commit_failure_clears_transfer_state(tool, tool_context, sample_call_session):
    tool_context.ari_client.continue_in_dialplan = AsyncMock(return_value=False)

    result = await tool.commit_deferred_action(
        {"target": "9255550123", "dialplan_context": "from-house"}, tool_context
    )

    assert result["status"] == "failed"
    assert sample_call_session.transfer_active is False
    assert sample_call_session.transfer_target is None


@pytest.mark.asyncio
async def test_unsafe_context_fails_closed(tool, tool_context, sample_call_session):
    tool_context.config["tools"]["dial_phone"] = {
        "dialplan_context": "from-house,1,Hangup"
    }
    await tool.execute({"phone_number": "9255550123", "confirmed": False}, tool_context)
    sample_call_session.conversation_history.append({"role": "user", "content": "Yes"})

    result = await tool.execute(
        {"phone_number": "9255550123", "confirmed": True}, tool_context
    )

    assert result["status"] == "failed"
    assert sample_call_session.pending_deferred_transfer is None
