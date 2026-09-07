import pytest

from src.core.operator_zero_state import OperatorZeroPhase, OperatorZeroTransferState


def _started_state():
    return OperatorZeroTransferState.start(
        {"type": "predial_transfer", "target": "100"}
    )


def test_happy_path_has_explicit_monotonic_phases_and_legacy_flags():
    state = _started_state()
    assert state.phase is OperatorZeroPhase.DIALING

    state = state.destination_answered("inside-1")
    assert state.phase is OperatorZeroPhase.ANSWERED
    assert state.action["answered"] is True

    state = state.caller_audio_drained().announcement_started()
    assert state.phase is OperatorZeroPhase.ANNOUNCING
    assert state.action["private_announcement_played"] is False

    state = state.announcement_finished(True)
    assert state.phase is OperatorZeroPhase.ANNOUNCED
    assert state.announcement_complete is True
    assert state.trust_eligible is False

    state = state.bridge_finished(True)
    assert state.phase is OperatorZeroPhase.BRIDGED
    assert state.action["bridged"] is True
    assert state.trust_eligible is True


def test_announcement_claim_is_not_completion_or_trust():
    state = _started_state().destination_answered("inside-1")
    state = state.caller_audio_drained().announcement_started()
    assert state.announcement_complete is False
    assert state.trust_eligible is False
    with pytest.raises(ValueError, match="already started"):
        state.announcement_started()


def test_late_or_duplicate_ready_and_answer_events_do_not_regress_phase():
    state = (
        _started_state()
        .destination_answered("inside-1")
        .caller_audio_drained()
        .announcement_started()
    )
    state = state.caller_audio_drained().destination_answered("inside-1")
    assert state.phase is OperatorZeroPhase.ANNOUNCING


def test_bridge_is_fail_closed_until_announcement_completes():
    state = _started_state().destination_answered("inside-1").caller_audio_drained()
    with pytest.raises(ValueError, match="before private announcement"):
        state.bridge_finished(True)


@pytest.mark.parametrize("completed", [False, True])
def test_failed_announcement_or_bridge_never_becomes_trust_eligible(completed):
    state = (
        _started_state()
        .destination_answered("inside-1")
        .caller_audio_drained()
        .announcement_started()
        .announcement_finished(completed)
    )
    if completed:
        state = state.bridge_finished(False)
    assert state.phase is OperatorZeroPhase.FAILED
    assert state.trust_eligible is False


def test_legacy_action_is_loaded_without_changing_legacy_evidence():
    state = OperatorZeroTransferState.load(
        {"answered": True, "private_announcement_played": True, "bridged": False}
    )
    assert state.phase is OperatorZeroPhase.ANNOUNCED
    assert state.announcement_complete is True
    assert state.trust_eligible is False
