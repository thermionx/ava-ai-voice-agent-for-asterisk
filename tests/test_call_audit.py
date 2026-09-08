import asyncio
import gzip
import json
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.core.call_audit.ari import normalize as ari_events
from src.core.call_audit.cel import normalize as cel_events
from src.core.call_audit.events import observation, timestamp
from src.core.call_audit.publisher import AuditPublisher, publish
from src.core.call_audit.store import AuditStore
from src.core.call_audit.worker import ingest_file, scan

CALL = "1700000000.123"
BASE = 1700000000.0


def event(kind, seconds=0, **details):
    return observation(CALL, kind, at=BASE + seconds, details=details)


@pytest.fixture
def store(tmp_path):
    result = AuditStore(tmp_path / "audit.db")
    result.initialize()
    return result


def seed(store):
    store.ingest(event("inbound_call_received", caller_number="+12025550123",
                       called_number="+12025550124", caller_name="Example Caller"))
    store.ingest(event("asterisk_answered", 0.3))


def test_native_asterisk_22_user_event_extra_wrapper_preserves_original_did():
    raw = {"eventtype": "OZ_INBOUND", "eventtime": BASE, "context": "operator-zero-audit-inbound",
           "channame": "PJSIP/twilio-00000001", "uniqueid": CALL, "linkedid": CALL,
           "exten": "s", "eventextra": '{"extra":"+12025550124"}'}
    events = cel_events(raw)
    assert events[0]["details"]["called_number"] == "+12025550124"
    raw.update(eventtype="OZ_TWILIO_SID", eventextra='{"extra":"CA-test-sid"}')
    assert cel_events(raw, known_call=True)[0]["details"]["twilio_call_sid"] == "CA-test-sid"


@pytest.mark.parametrize("name,observations,outcome,initiator", [
    ("completed", [event("call_completed", 8), event("channel_ended", 9, disconnect_initiator="caller_hung_up")], "completed", "caller_hung_up"),
    ("greeting_hangup", [event("operator_zero_greeting_started", 1), event("channel_ended", 2, disconnect_initiator="caller_hung_up")], "caller_hung_up_during_greeting", "caller_hung_up"),
    ("conversation_hangup", [event("operator_zero_greeting_finished", 2), event("caller_speech_started", 3), event("channel_ended", 4, disconnect_initiator="caller_hung_up")], "caller_hung_up_during_conversation", "caller_hung_up"),
    ("few_words", [event("caller_speech_started", 1), event("channel_ended", 1.8, disconnect_initiator="caller_hung_up")], "caller_hung_up_during_conversation", "caller_hung_up"),
    ("no_speech", [event("speech_analysis_completed", 3, caller_spoke=False), event("channel_ended", 3.1)], "no_speech", "unknown_disconnect"),
    ("transfer", [event("transfer_completed", 4), event("channel_ended", 30)], "transferred_to_inside_phone", "unknown_disconnect"),
    ("operator_hangup", [event("hangup_requested", 4), event("hangup_accepted", 4.1), event("channel_ended", 4.2, hangup_cause=16)], "operator_zero_terminated", "operator_zero_hung_up"),
    ("transcription_failure", [event("transcription_failed", 2), event("channel_ended", 3)], "unknown", "unknown_disconnect"),
    ("recording_failure", [event("recording_failed", 0.4), event("call_completed", 4), event("channel_ended", 5)], "completed", "unknown_disconnect"),
    ("sip_failure", [event("channel_ended", 2, disconnect_initiator="remote_sip_failure", hangup_cause=21)], "failed", "remote_sip_failure"),
    ("network_failure", [event("channel_ended", 2, disconnect_initiator="network_failure", hangup_cause=102)], "failed", "network_failure"),
    ("asterisk_hangup", [event("channel_ended", 2, disconnect_initiator="asterisk_hung_up")], "unknown", "asterisk_hung_up"),
])
def test_lifecycle_outcomes(store, name, observations, outcome, initiator):
    seed(store)
    for item in observations:
        store.ingest(item)
    call = store.get(CALL)
    assert call["outcome"] == outcome, name
    assert call["disconnect_initiator"] == initiator
    assert call["duration_seconds"] > 0
    assert call["called_number"] == "+12025550124"


def test_two_second_abandonment_without_ai_session_has_original_metadata(store):
    seed(store)
    store.ingest(event("channel_ended", 2.3, hangup_cause=16))
    call = store.get(CALL)
    assert call["caller_number"] == "+12025550123"
    assert call["duration_seconds"] == pytest.approx(2.3)
    assert call["answered_duration_seconds"] == pytest.approx(2.0)
    assert call["operator_zero_started"] == 0
    assert call["caller_spoke"] is None
    assert call["disconnect_initiator"] == "unknown_disconnect"


def test_transfer_stasis_exit_does_not_finalize_the_telephone_call(store):
    seed(store)
    store.ingest(event("transfer_completed", 4))
    store.ingest(event("ai_session_ended", 4.1))
    assert store.get(CALL)["ended_at"] is None
    store.ingest(event("channel_ended", 65))
    assert store.get(CALL)["duration_seconds"] == 65


def test_late_events_repair_early_partial_record_idempotently(store):
    end = event("channel_ended", 3, hangup_cause=16)
    store.ingest(end)
    assert store.get(CALL)["duration_seconds"] is None
    seed(store)
    store.ingest(end)
    store.ingest(event("caller_speech_started", 1))
    assert store.get(CALL)["duration_seconds"] == 3
    assert len(store.get(CALL)["events"]) == 4
    assert store.get(CALL)["caller_spoke"] == 1


def test_called_number_marker_wins_over_alias_and_survives_reordering(store):
    store.ingest(event("channel_metadata", 0.1, called_number="+12025550124", original_metadata=True))
    store.ingest(event("inbound_call_received", called_number="+12025550199"))
    assert store.get(CALL)["called_number"] == "+12025550124"


def test_metadata_and_events_are_retained_after_reinitialization(store):
    seed(store)
    store.initialize()
    reopened = AuditStore(store.path)
    assert reopened.get(CALL)["caller_name"] == "Example Caller"
    with reopened.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM audit_schema_migrations").fetchone()[0] == 1


def test_failed_hangup_and_cleanup_404_do_not_claim_operator_termination(store):
    seed(store)
    store.ingest(event("channel_ended", 2, disconnect_initiator="caller_hung_up"))
    store.ingest(event("hangup_requested", 2.1))
    store.ingest(event("hangup_failed", 2.2, http_status=404))
    assert store.get(CALL)["disconnect_initiator"] == "caller_hung_up"


def test_accepted_hangup_response_after_destroy_uses_request_timestamp(store):
    seed(store)
    store.ingest(event("channel_ended", 2, hangup_cause=16))
    store.ingest(event("hangup_accepted", 2.1, requested_at=timestamp(BASE + 1.9)))
    assert store.get(CALL)["disconnect_initiator"] == "operator_zero_hung_up"


def test_native_cel_replay_covers_call_without_any_ava_events(store, tmp_path):
    path = tmp_path / "operator-zero.jsonl"
    base = {"uniqueid": CALL, "linkedid": CALL, "channame": "PJSIP/twilio-0001",
            "context": "from-twilio", "exten": "+12025550124", "num": "+12025550123", "name": "Example Caller"}
    rows = [dict(base, eventtype="CHAN_START", eventtime=BASE),
            dict(base, eventtype="ANSWER", eventtime=BASE + .3),
            dict(base, context="operator-zero-voicemail", eventtype="HANGUP", eventtime=BASE + 2,
                 eventextra=json.dumps({"hangupcause": 16, "hangupsource": "PJSIP/twilio-0001"}))]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    ingest_file(store, path, "cel")
    call = store.get(CALL)
    assert call["duration_seconds"] == 2
    assert call["disconnect_initiator"] == "caller_hung_up"
    assert call["operator_zero_started"] == 0


def test_native_ingress_marker_keeps_original_called_number_inside_gosub():
    raw = {"eventtype": "OZ_INBOUND", "eventtime": BASE, "uniqueid": CALL,
           "channame": "PJSIP/twilio-0001", "context": "operator-zero-audit-inbound",
           "exten": "s", "eventextra": "+12025550124"}
    assert cel_events(raw)[0]["details"]["called_number"] == "+12025550124"


@pytest.mark.parametrize("channel,context", [("PJSIP/twilio-0002", "from-house"),
                                            ("AudioSocket/helper", "from-twilio"),
                                            ("Local/100", "from-twilio")])
def test_outbound_and_auxiliary_channels_do_not_create_inbound_calls(channel, context):
    raw = {"type": "ChannelCreated", "timestamp": timestamp(BASE),
           "channel": {"id": CALL, "name": channel, "dialplan": {"context": context}}}
    assert ari_events(raw, {}) == []
    assert cel_events({"eventtype": "CHAN_START", "eventtime": BASE, "uniqueid": CALL,
                       "channame": channel, "context": context}) == []


def test_ari_metadata_end_and_speech_are_independent_of_session():
    known = {}
    channel = {"id": CALL, "name": "PJSIP/twilio-0001", "creationtime": timestamp(BASE),
               "caller": {"number": "+12025550123"},
               "dialplan": {"context": "from-twilio", "exten": "+12025550124"}}
    received = ari_events({"type": "ChannelCreated", "timestamp": timestamp(BASE), "channel": channel}, known)
    assert received[0]["event_type"] == "inbound_call_received"
    channel["dialplan"]["context"] = "operator-zero-transfer"
    destroyed = ari_events({"type": "ChannelDestroyed", "timestamp": timestamp(BASE + 2), "channel": channel,
                            "cause": 16, "cause_txt": "Normal Clearing"}, known)
    assert destroyed[0]["event_type"] == "channel_ended"
    assert "disconnect_initiator" not in destroyed[0]["details"]


def test_database_lock_does_not_advance_journal_checkpoint(store, tmp_path):
    path = tmp_path / "engine-test.jsonl"
    path.write_text(json.dumps(event("inbound_call_received")) + "\n")
    locked = sqlite3.connect(store.path)
    try:
        locked.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError):
            ingest_file(store, path, "engine")
    finally:
        locked.rollback()
        locked.close()
    ingest_file(store, path, "engine")
    assert store.get(CALL) is not None


def test_partial_append_rotation_and_restart_replay_are_deduplicated(store, tmp_path):
    path = tmp_path / "engine-test.jsonl"
    first = json.dumps(event("inbound_call_received")) + "\n"
    last = json.dumps(event("channel_ended", 2)) + "\n"
    path.write_text(first + last[:15])
    ingest_file(store, path, "engine")
    assert store.get(CALL)["ended_at"] is None
    with path.open("a") as file:
        file.write(last[15:])
    ingest_file(store, path, "engine")
    with gzip.open(tmp_path / "engine-test.jsonl.1.gz", "wt") as file:
        file.write(first + last)
    scan(store, tmp_path)
    assert len(store.get(CALL)["events"]) == 2


def test_invalid_record_does_not_block_later_calls(store, tmp_path, caplog):
    path = tmp_path / "engine-test.jsonl"
    path.write_text("bad json\n" + json.dumps(event("inbound_call_received")) + "\n")
    ingest_file(store, path, "engine")
    assert store.get(CALL) is not None
    assert "Invalid audit journal record" in caplog.text


def test_oversized_record_does_not_block_subsequent_calls(store, tmp_path):
    path = tmp_path / "engine-test.jsonl"
    path.write_text("x" * 100000 + "\n" + json.dumps(event("inbound_call_received")) + "\n")
    ingest_file(store, path, "engine")
    assert store.get(CALL) is not None


def test_newer_schema_is_rejected_before_modification(tmp_path):
    path = tmp_path / "future.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE audit_schema_migrations(version INTEGER)")
        db.execute("INSERT INTO audit_schema_migrations VALUES (2)")
    with pytest.raises(ValueError, match="newer"):
        AuditStore(path).initialize()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [("audit_schema_migrations",)]


@pytest.mark.asyncio
async def test_publisher_writes_replayable_private_journal(store, tmp_path):
    journal = tmp_path / "journal"
    publisher = AuditPublisher(journal)
    publisher.start()
    try:
        publisher.known[CALL] = "PJSIP/twilio-0001"
        assert publisher.emit(CALL, "inbound_call_received", caller_number="+12025550123")
        await asyncio.wait_for(asyncio.to_thread(publisher.queue.join), timeout=2)
    finally:
        publisher.close()
    assert scan(store, journal) == 0
    assert store.get(CALL)["caller_number"] == "+12025550123"
    assert next(journal.glob("*.jsonl")).stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_disk_failure_remains_off_the_telephone_event_loop(tmp_path, monkeypatch):
    publisher = AuditPublisher(tmp_path / "journal")
    attempted = threading.Event()
    def fail_open(*args, **kwargs):
        attempted.set()
        raise OSError("disk unavailable")
    monkeypatch.setattr("src.core.call_audit.publisher.os.open", fail_open)
    publisher.start()
    try:
        raw = {"type": "ChannelCreated", "timestamp": timestamp(BASE), "channel": {
            "id": CALL, "name": "PJSIP/twilio-0001", "dialplan": {"context": "from-twilio"}}}
        await asyncio.wait_for(publisher.observe_ari(raw), timeout=.5)
        assert await asyncio.to_thread(attempted.wait, 1)
        owner = SimpleNamespace(call_audit=publisher)
        publish(owner, CALL, "operator_zero_session_started")
        # Work remains queued for retry; the caller's control path returned.
        assert publisher.queue.qsize() == 1
    finally:
        publisher.close()


def test_publisher_disabled_has_no_disk_effects(monkeypatch, tmp_path):
    monkeypatch.delenv("CALL_AUDIT_ENABLED", raising=False)
    monkeypatch.setenv("CALL_AUDIT_JOURNAL_DIR", str(tmp_path / "absent"))
    assert AuditPublisher.from_env() is None
    assert not (tmp_path / "absent").exists()


def test_queue_full_and_broken_observer_cannot_raise_into_call_control(tmp_path):
    publisher = AuditPublisher(tmp_path, capacity=1)
    publisher.known[CALL] = "PJSIP/twilio-0001"
    assert publisher.emit(CALL, "operator_zero_session_started")
    assert not publisher.emit(CALL, "caller_speech_started")
    assert publisher.dropped == 1
    owner = SimpleNamespace(call_audit=SimpleNamespace(emit=Mock(side_effect=OSError("disk full"))))
    publish(owner, CALL, "operator_zero_session_started")


@pytest.mark.asyncio
@pytest.mark.parametrize("http_status,expected_event", [(204, "hangup_accepted"), (404, "hangup_failed"), (500, "hangup_failed")])
async def test_real_ari_hangup_observer_preserves_existing_control_contract(http_status, expected_event):
    from src.ari_client import ARIClient
    client = ARIClient("test", "test", "http://localhost:8088/ari", "test")
    client.send_command = AsyncMock(return_value={"status": http_status})
    client.call_audit = SimpleNamespace(emit=Mock())
    result = await client.hangup_channel(CALL)
    assert result is (http_status in {204, 404})
    assert client.call_audit.emit.call_args_list[-1].args[1] == expected_event
    client.call_audit.emit.side_effect = OSError("audit failure")
    assert await client.hangup_channel(CALL) is result


@pytest.mark.asyncio
async def test_real_terminal_controller_hangs_up_once_even_when_audit_fails():
    from src.engine import Engine
    from src.core.models import CallSession
    engine = Engine.__new__(Engine)
    session = CallSession(call_id=CALL, caller_channel_id=CALL)
    engine.session_store = SimpleNamespace(get_by_call_id=AsyncMock(return_value=session))
    engine.ari_client = SimpleNamespace(hangup_channel=AsyncMock(return_value=True))
    engine.call_audit = SimpleNamespace(emit=Mock(side_effect=OSError("unavailable")))
    engine._save_session = AsyncMock()
    engine.config = SimpleNamespace(audio_transport="audiosocket")
    assert await engine._terminate_call_after_audio(CALL, reason="farewell_completed", audio_already_drained=True)
    assert not await engine._terminate_call_after_audio(CALL, reason="farewell_completed", audio_already_drained=True)
    engine.ari_client.hangup_channel.assert_awaited_once_with(CALL)


@pytest.mark.asyncio
async def test_real_stasis_end_keeps_existing_cleanup_owner():
    from src.engine import Engine
    engine = Engine.__new__(Engine)
    engine._outbound_awaiting_amd_channel_ids = set()
    engine._cleanup_call = AsyncMock()
    await engine._handle_stasis_end({"channel": {"id": CALL}})
    engine._cleanup_call.assert_awaited_once_with(CALL)
