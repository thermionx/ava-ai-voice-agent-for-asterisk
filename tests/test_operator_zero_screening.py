from src.core.operator_zero_screening import ScreeningFacts, value_was_spoken
import pytest


@pytest.mark.parametrize('reason', [
    'I want to talk with Brian', "I'm a friend", 'we met once',
    "I'm looking for Brian", "It's personal", 'just catching up',
    "calling about tomorrow's lunch", "a delivery", "an appointment",
])
def test_broad_reasons_allow_same_name_transfer(reason):
    facts = ScreeningFacts('Brian', 'Brian', reason=reason)
    history = [{'role': 'user', 'content': 'My name is Brian. Brian, please. ' + reason}]
    assert facts.next_action(history).action == 'TRANSFER'
    assert facts.announcement == 'Brian is on the line. Reason for calling: ' + reason


@pytest.mark.parametrize('reason', ['', "I'd rather not say", "I'd rather not say why. Please connect me.",
                                    'none of your business', 'No', 'declined', 'I do not want to say'])
def test_no_reason_or_refusal_is_not_admitted(reason):
    facts = ScreeningFacts('Alex', 'Brian', reason=reason)
    assert facts.next_action([{'role': 'user', 'content': 'Alex calling for Brian. ' + reason}]).action == 'ASK_REASON'


def test_invented_or_assistant_only_reason_is_not_admitted():
    facts = ScreeningFacts('Alex', 'Brian', reason='lunch')
    history = [{'role': 'user', 'content': 'Alex calling for Brian'},
               {'role': 'assistant', 'content': 'Is this about lunch?'}]
    assert facts.next_action(history).action == 'ASK_REASON'


def test_spelling_aliases_do_not_change_reason_evidence():
    facts = ScreeningFacts('Katherine', 'Brian', reason='talk to Brian')
    history = [{'role': 'user', 'content': 'Catherine. I want to talk to Bryan.'}]
    assert facts.next_action(history, {'Katherine': ['Catherine'], 'Brian': ['Bryan']}).action == 'ASK_REASON'


def test_missing_facts_have_distinct_next_actions():
    history = [{'role': 'user', 'content': 'Alex calling for Brian about lunch'}]
    assert ScreeningFacts(recipient='Brian', reason='lunch').next_action(history).action == 'ASK_CALLER'
    assert ScreeningFacts(caller_name='Alex', reason='lunch').next_action(history).action == 'ASK_RECIPIENT'
    assert ScreeningFacts('Alex', 'Brian').next_action(history).action == 'ASK_REASON'


def test_screening_record_survives_transfer_lifecycle_and_action_serialization():
    from src.core.models import CallSession
    from src.core.operator_zero_state import OperatorZeroTransferState
    facts = ScreeningFacts('Alex', 'Brian', 'Acme', 'we met once')
    state = OperatorZeroTransferState.start({'screening_facts': facts.to_metadata()})
    state = state.destination_answered('inside').caller_audio_drained().announcement_started().announcement_finished(True).bridge_finished(True)
    session = CallSession(call_id='screening-restore', caller_channel_id='outside', current_action=state.action)
    import json
    restored = CallSession(call_id=session.call_id, caller_channel_id=session.caller_channel_id,
                           current_action=json.loads(json.dumps(session.current_action)))
    assert ScreeningFacts.from_action(restored.current_action) == facts
    assert facts.announcement == 'Alex from Acme is on the line. Reason for calling: we met once'


@pytest.mark.parametrize('reason', ['', "I'd rather not say", 'invented appointment'])
def test_spoken_business_is_enough_without_a_separate_reason(reason):
    facts = ScreeningFacts('Alex', 'Brian', 'Acme Plumbing', reason)
    history = [{'role': 'user', 'content': "I'm Alex from Acme Plumbing. Brian, please. I'd rather not say."}]
    assert facts.next_action(history).action == 'TRANSFER'


@pytest.mark.parametrize('history', [
    [{'role': 'user', 'content': 'Alex. Brian, please.'}, {'role': 'assistant', 'content': 'Acme Plumbing?'}],
    [{'role': 'user', 'content': 'Alex. Brian, please.'}],
])
def test_business_must_be_caller_spoken(history):
    assert ScreeningFacts('Alex', 'Brian', 'Acme Plumbing').next_action(history).action == 'ASK_REASON'


def test_business_does_not_waive_names_or_official_purpose():
    history = [{'role': 'user', 'content': 'Alex from Acme Plumbing. Brian, please.'}]
    assert ScreeningFacts(recipient='Brian', company='Acme Plumbing').next_action(history).action == 'ASK_CALLER'
    assert ScreeningFacts(caller_name='Alex', company='Acme Plumbing').next_action(history).action == 'ASK_RECIPIENT'
    assert ScreeningFacts(company='Lafayette Police').next_action([{'role': 'user', 'content': 'Lafayette Police'}]).action == 'ASK_RECIPIENT'


@pytest.mark.parametrize('reason', ["I'd rather not say", "I'd rather not say more"])
def test_official_service_still_needs_a_nonrefused_purpose(reason):
    facts = ScreeningFacts(company='Lafayette Police', reason=reason)
    assert facts.next_action([{'role': 'user', 'content': 'Lafayette Police. ' + reason}]).action == 'ASK_RECIPIENT'


@pytest.mark.parametrize('reason', ['a delivery', 'delivering a package', 'deliveries', 'an appointment', 'appointments'])
def test_delivery_or_appointment_business_replaces_personal_name(reason):
    facts = ScreeningFacts(recipient='Brian', company='Acme', reason=reason)
    history = [{'role': 'user', 'content': 'Acme calling for Brian about ' + reason}]
    assert facts.next_action(history).action == 'TRANSFER'
    assert facts.announcement == 'Acme is on the line. Reason for calling: ' + reason


@pytest.mark.parametrize('company,reason,spoken,expected', [
    ('Acme', 'a delivery', 'Brian about a delivery', 'ASK_CALLER'),
    ('Acme', 'an appointment', 'Acme for Brian', 'ASK_CALLER'),
    ('Acme', 'a sales call', 'Acme for Brian about a sales call', 'ASK_CALLER'),
    ('', 'a delivery', 'Brian about a delivery', 'ASK_CALLER'),
    ('Acme', 'a delivery', 'Acme about a delivery', 'ASK_RECIPIENT'),
])
def test_business_identity_exception_requires_supported_business_purpose_and_recipient(company, reason, spoken, expected):
    facts = ScreeningFacts(recipient='Brian', company=company, reason=reason)
    assert facts.next_action([{'role': 'user', 'content': spoken}]).action == expected
