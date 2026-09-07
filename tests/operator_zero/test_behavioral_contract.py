import pytest

from src.tools.telephony.unified_transfer import UnifiedTransferTool
from tests.operator_zero.call_simulator import CallerMemory, OperatorZeroCall, State


def test_external_screening_to_single_announcement_and_transfer():
    call = OperatorZeroCall()
    call.start()
    call.identify("Bob")
    call.request_recipient("Brian")
    call.transfer("Bob from Acme Home Repair for Brian.")

    assert call.spoken == [
        "Operator Zero. How may I help you?",
        "One moment, please.",
    ]
    assert call.effects == [
        "announce:Bob from Acme Home Repair for Brian.",
        "blind_transfer:inside_phone",
    ]
    assert call.state is State.CONNECTED
    assert {"caller_channel", "inside_channel", "mixing_bridge"} <= call.resources


def test_name_and_specific_recipient_are_sufficient_without_purpose():
    history = [{"role": "user", "content": "This is Bob. I need Brian."}]
    assert UnifiedTransferTool._validate_operator_zero_screening(
        {"caller_name": "Bob", "recipient": "Brian", "reason": ""}, history
    ) is None


def test_unknown_caller_cannot_use_generic_household_role():
    call = OperatorZeroCall()
    call.start()
    call.identify("Bob")
    call.request_recipient("head of household", specific=False)
    with pytest.raises(RuntimeError, match="screening incomplete"):
        call.transfer("Bob for the household.")


def test_confirmed_official_purpose_allows_no_named_recipient():
    history = [{"role": "user", "content": "Lafayette Police calling about an emergency welfare check."}]
    assert UnifiedTransferTool._validate_operator_zero_screening(
        {"company": "Lafayette Police", "reason": "emergency welfare check"}, history
    ) is None


@pytest.mark.parametrize("source", ["ai", "tool", "transfer"])
def test_failures_terminate_and_release_every_resource(source):
    call = OperatorZeroCall()
    call.start()
    call.fail(source)
    assert call.state is State.ENDED
    assert call.resources == set()
    assert call.effects[-1] == f"hangup:{source}"


def test_outside_timeout_terminates_and_releases_resources():
    call = OperatorZeroCall()
    call.start()
    call.timeout()
    assert call.state is State.ENDED
    assert call.resources == set()


def test_caller_hangup_is_idempotent_and_cleans_resources():
    call = OperatorZeroCall()
    call.start()
    call.hangup()
    call.hangup()
    assert call.state is State.ENDED
    assert call.resources == set()


def test_reopened_conversation_gets_one_final_goodbye_then_machine_hangs_up():
    call = OperatorZeroCall(external=False)
    call.start()
    call.state = State.CONNECTED
    call.close()
    call.reopen()
    call.close()
    assert call.goodbye_count == 2
    assert call.spoken == ["Goodbye.", "Goodbye."]
    assert call.state is State.ENDED
    assert call.resources == set()


def test_internal_dial_zero_has_no_external_screening_greeting():
    call = OperatorZeroCall(external=False)
    call.start()
    assert call.state is State.SCREENING
    assert call.spoken == []


def test_known_trusted_caller_bypasses_screening_but_new_caller_does_not():
    memory = CallerMemory()
    memory.seen("15551230000", "Alice")
    assert memory.recent_caller()["trusted"] is False
    memory.manage_caller("15551230000", "trust")
    assert memory.recent_caller()["trusted"] is True
    memory.seen("15559870000", "New caller")
    assert memory.recent_caller()["trusted"] is False


def test_recent_caller_and_manage_caller_trust_untrust_and_block():
    memory = CallerMemory()
    memory.seen("15551230000", "Alice")
    assert memory.recent_caller()["phone_number"] == "15551230000"
    memory.manage_caller("15551230000", "trust")
    assert memory.recent_caller() | {} == {
        "name": "Alice", "trusted": True, "blocked": False,
        "phone_number": "15551230000",
    }
    memory.manage_caller("15551230000", "untrust")
    assert memory.recent_caller()["trusted"] is False
    memory.manage_caller("15551230000", "block")
    assert memory.recent_caller()["blocked"] is True


def test_voicemail_handoff_releases_ai_media_without_hanging_up_caller():
    call = OperatorZeroCall()
    call.start()
    call.voicemail()
    assert call.effects == ["continue:operator-zero-voicemail"]
    assert call.resources == {"caller_channel"}


def test_audio_is_bidirectional_during_screening_and_after_connection():
    call = OperatorZeroCall()
    call.start()
    assert call.audio_path() == (True, True)
    call.identify("Bob")
    call.request_recipient("Brian")
    call.transfer("Bob for Brian.")
    assert call.audio_path() == (True, True)
