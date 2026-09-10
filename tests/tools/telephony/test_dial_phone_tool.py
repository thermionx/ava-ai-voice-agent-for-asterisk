import time
from unittest.mock import AsyncMock

import pytest

from src.tools.telephony import dial_phone as dial_phone_module
from src.tools.telephony.deferred_transfer import DEFERRED_TRANSFER_RESULT_KEY
from src.tools.telephony.dial_phone import (
    DialPhoneTool,
    ListDialedNumbersTool,
    list_outbound_calls,
    normalize_nanp_number,
    record_outbound_call,
)


@pytest.fixture
def tool(untrusted_number_lookup):
    return DialPhoneTool()


@pytest.fixture
def untrusted_number_lookup(monkeypatch):
    monkeypatch.setattr(dial_phone_module, "_number_is_trusted", lambda _number: False)
    monkeypatch.setattr(
        dial_phone_module,
        "_number_is_in_agent_profile",
        lambda _number, _context_name: False,
    )


def test_normalize_nanp_number_accepts_common_formats():
    assert normalize_nanp_number("(925) 555-0123") == "9255550123"
    assert normalize_nanp_number("+1 925 555 0123") == "19255550123"
    assert (
        normalize_nanp_number("555-0123", default_area_code="415")
        == "4155550123"
    )


def test_normalize_nanp_number_does_not_expand_without_valid_default_area_code():
    assert normalize_nanp_number("555-0123") is None
    assert normalize_nanp_number("555-0123", default_area_code="12") is None
    assert normalize_nanp_number("555-0123", default_area_code="125") is None


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
async def test_seven_digit_number_uses_private_environment_area_code(
    tool, tool_context, sample_call_session, monkeypatch
):
    monkeypatch.setenv("OPERATOR_ZERO_DEFAULT_AREA_CODE", "415")
    result = await tool.execute(
        {"phone_number": "555-0123", "confirmed": False},
        tool_context,
    )

    assert result["status"] == "confirmation_required"
    assert result["phone_number"] == "4155550123"
    assert sample_call_session.pending_phone_call["phone_number"] == "4155550123"


@pytest.mark.asyncio
async def test_first_invocation_marked_confirmed_still_only_stages(tool, tool_context, sample_call_session):
    result = await tool.execute(
        {"phone_number": "9255550123", "confirmed": True},
        tool_context,
    )

    assert result["status"] == "confirmation_required"
    assert sample_call_session.pending_deferred_transfer is None


@pytest.mark.asyncio
async def test_verified_trusted_number_skips_readback(
    tool, tool_context, sample_call_session, monkeypatch
):
    monkeypatch.setattr(dial_phone_module, "_number_is_trusted", lambda _number: True)

    result = await tool.execute(
        {"phone_number": "9255550123", "confirmed": False, "display_name": "Pat"},
        tool_context,
    )

    assert result["status"] == "success"
    assert result["message"] == "Calling Pat now."
    assert result["trusted_number"] is True
    assert sample_call_session.pending_phone_call is None
    assert sample_call_session.pending_deferred_transfer is None
    tool_context.ari_client.continue_in_dialplan.assert_awaited_once()


@pytest.mark.asyncio
async def test_agent_profile_number_skips_readback(
    tool, tool_context, sample_call_session, monkeypatch
):
    tool_context.context_name = "operator_zero"
    monkeypatch.setattr(
        dial_phone_module,
        "_number_is_in_agent_profile",
        lambda number, context_name: (
            number == "9255550123" and context_name == "operator_zero"
        ),
    )

    result = await tool.execute(
        {"phone_number": "9255550123", "confirmed": False, "display_name": "Pat"},
        tool_context,
    )

    assert result["status"] == "success"
    assert result["approval_source"] == "agent_profile"
    assert sample_call_session.pending_phone_call is None
    assert sample_call_session.pending_deferred_transfer is None
    tool_context.ari_client.continue_in_dialplan.assert_awaited_once()


def test_agent_profile_lookup_matches_only_active_operator_zero(tmp_path, monkeypatch):
    db_path = str(tmp_path / "agents.db")
    with dial_phone_module.sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE agents (slug TEXT, prompt TEXT, is_active INTEGER)"
        )
        connection.execute(
            "INSERT INTO agents VALUES (?, ?, ?)",
            ("operator_zero", "Brian mobile: 925.555.0123.", 1),
        )
    monkeypatch.setenv("OPERATOR_ZERO_AGENTS_DB", db_path)

    assert dial_phone_module._number_is_in_agent_profile(
        "9255550123", "operator_zero"
    )
    assert not dial_phone_module._number_is_in_agent_profile(
        "9255550123", "operator_zero_incoming"
    )
    assert dial_phone_module._agent_profile_number_for_name(
        "Brian", "operator_zero"
    ) == "9255550123"
    assert not dial_phone_module._number_is_in_agent_profile(
        "9255550199", "operator_zero"
    )
    with dial_phone_module.sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE agents SET is_active = 0")
    assert not dial_phone_module._number_is_in_agent_profile(
        "9255550123", "operator_zero"
    )


def test_agent_profile_lookup_resolves_spoken_nickname_and_mobile_noise(
    tmp_path, monkeypatch
):
    db_path = str(tmp_path / "agents.db")
    with dial_phone_module.sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE agents (slug TEXT, prompt TEXT, is_active INTEGER)"
        )
        connection.execute(
            "INSERT INTO agents VALUES (?, ?, ?)",
            (
                "operator_zero",
                "Lilian's mobile number is 925.555.0123.",
                1,
            ),
        )
    monkeypatch.setenv("OPERATOR_ZERO_AGENTS_DB", db_path)

    assert dial_phone_module._agent_profile_number_for_name(
        "Little Lili mobile phone", "operator_zero"
    ) == "9255550123"


def test_agent_profile_lookup_fails_closed_for_ambiguous_prefix(tmp_path, monkeypatch):
    db_path = str(tmp_path / "agents.db")
    with dial_phone_module.sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE agents (slug TEXT, prompt TEXT, is_active INTEGER)"
        )
        connection.execute(
            "INSERT INTO agents VALUES (?, ?, ?)",
            (
                "operator_zero",
                "Lilian mobile: 925.555.0123.\nLilibeth mobile: 925.555.0199.",
                1,
            ),
        )
    monkeypatch.setenv("OPERATOR_ZERO_AGENTS_DB", db_path)

    assert (
        dial_phone_module._agent_profile_number_for_name(
            "Lili mobile", "operator_zero"
        )
        is None
    )


@pytest.mark.asyncio
async def test_named_agent_profile_number_overrides_conflicting_whitelist_number(
    tool, tool_context, sample_call_session, monkeypatch
):
    tool_context.context_name = "operator_zero"
    monkeypatch.setattr(dial_phone_module, "_number_is_trusted", lambda _number: True)
    monkeypatch.setattr(
        dial_phone_module,
        "_agent_profile_number_for_name",
        lambda name, context_name: "9255550123",
    )
    monkeypatch.setattr(
        dial_phone_module,
        "_number_is_in_agent_profile",
        lambda number, context_name: number == "9255550123",
    )

    result = await tool.execute(
        {
            "phone_number": "9255550199",
            "confirmed": True,
            "display_name": "Brian",
        },
        tool_context,
    )

    assert result["status"] == "success"
    assert result["phone_number"] == "9255550123"
    tool_context.ari_client.continue_in_dialplan.assert_awaited_once_with(
        tool_context.caller_channel_id,
        context="from-house",
        extension="9255550123",
        priority=1,
    )


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
    assert sample_call_session.pending_deferred_transfer == {
        **action,
        "armed_user_turn_count": 2,
    }
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
async def test_commit_revalidates_target_continues_and_records_history(
    tool, tool_context, tmp_path, monkeypatch
):
    db_path = str(tmp_path / "outbound.db")
    monkeypatch.setenv("OPERATOR_ZERO_OUTBOUND_HISTORY_DB", db_path)
    action = {
        "target": "9255550123",
        "dialplan_context": "from-house",
        "payload": {"display_name": "Pat"},
    }

    result = await tool.commit_deferred_action(action, tool_context)

    assert result["status"] == "success"
    tool_context.ari_client.continue_in_dialplan.assert_awaited_once_with(
        tool_context.caller_channel_id,
        context="from-house",
        extension="9255550123",
        priority=1,
    )
    calls = list_outbound_calls(db_path, limit=10)
    assert calls[0]["phone_number"] == "9255550123"
    assert calls[0]["display_name"] == "Pat"


def test_outbound_history_is_append_only_and_newest_first(tmp_path):
    db_path = str(tmp_path / "outbound.db")
    record_outbound_call(
        db_path,
        phone_number="9255550101",
        display_name="First",
        call_id="call-1",
    )
    record_outbound_call(
        db_path,
        phone_number="9255550102",
        display_name="Second",
        call_id="call-2",
    )
    record_outbound_call(
        db_path,
        phone_number="9255550102",
        display_name="Duplicate retry",
        call_id="call-2",
    )

    calls = list_outbound_calls(db_path, limit=10)

    assert [call["phone_number"] for call in calls] == [
        "9255550102",
        "9255550101",
    ]


@pytest.mark.asyncio
async def test_list_dialed_numbers_uses_separate_outbound_history(
    tmp_path, monkeypatch, tool_context
):
    db_path = str(tmp_path / "outbound.db")
    monkeypatch.setenv("OPERATOR_ZERO_OUTBOUND_HISTORY_DB", db_path)
    record_outbound_call(
        db_path,
        phone_number="9255550101",
        display_name="Pat",
        call_id="call-1",
    )

    result = await ListDialedNumbersTool().execute({"limit": 20}, tool_context)

    assert result["status"] == "success"
    assert result["count"] == 1
    assert result["calls"][0]["display_name"] == "Pat"


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


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["trusted", "profile"])
async def test_household_contact_announcement_precedes_dial(
    tool, tool_context, sample_call_session, monkeypatch, source
):
    tool_context.context_name = "operator_zero"
    tool_context.config["tools"]["dial_phone"] = {"household_dial_announcements": True}
    monkeypatch.setattr(dial_phone_module, "_number_is_trusted", lambda n: source == "trusted")
    monkeypatch.setattr(dial_phone_module, "_number_is_in_agent_profile", lambda n, c: source == "profile")
    result = await tool.execute(
        {"phone_number": "9255550123", "display_name": "Pat", "confirmed": False, "number_source": "contact"},
        tool_context,
    )
    assert result["message"] == "Calling Pat now."
    assert result[DEFERRED_TRANSFER_RESULT_KEY]["target"] == "9255550123"
    assert sample_call_session.pending_deferred_transfer is not None
    tool_context.ari_client.continue_in_dialplan.assert_not_awaited()


@pytest.mark.asyncio
async def test_dictated_number_readback_arms_dial_without_confirmation(
    tool, tool_context, sample_call_session, monkeypatch
):
    tool_context.context_name = "operator_zero"
    tool_context.config["tools"]["dial_phone"] = {"household_dial_announcements": True}
    monkeypatch.setattr(dial_phone_module, "_agent_profile_number_for_name", lambda name, ctx: "9255550999" if name else None)
    result = await tool.execute(
        {"phone_number": "(925) 555-0123", "display_name": "Pat", "confirmed": False, "number_source": "user"},
        tool_context,
    )
    assert result["message"] == "Calling 9 2 5 5 5 5 0 1 2 3 now."
    assert result[DEFERRED_TRANSFER_RESULT_KEY]["target"] == "9255550123"
    assert sample_call_session.pending_phone_call is None
    tool_context.ari_client.continue_in_dialplan.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("context_name,enabled,source", [
    ("operator_zero_incoming", True, "user"),
    ("operator_zero", False, "user"),
    ("operator_zero", "true", "user"),
    ("operator_zero", True, "directory"),
    ("operator_zero", True, None),
])
async def test_announcement_policy_keeps_other_confirmation_paths(
    tool, tool_context, context_name, enabled, source
):
    tool_context.context_name = context_name
    tool_context.config["tools"]["dial_phone"] = {"household_dial_announcements": enabled}
    result = await tool.execute(
        {"phone_number": "9255550123", "confirmed": False, "number_source": source}, tool_context
    )
    assert result["status"] == "confirmation_required"
    tool_context.ari_client.continue_in_dialplan.assert_not_awaited()
