import asyncio
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import wave

import pytest

from src.core.call_audit.cel import normalize
from src.core.call_audit.events import observation, timestamp
from src.core.call_audit.media import MediaWorker, inspect_wav, recording_path, uncovered_intervals
from src.core.call_audit.publisher import AuditPublisher
from src.core.call_audit.realtime import RealtimeTranscriptObserver
from src.core.call_audit.store import AuditStore
from src.core.call_audit.transcription import Utterance, OpenAIRecordingTranscriber
from src.core.call_audit.worker import scan

CALL, BASE = "1700000000.456", 1700000000.0


def event(kind, at=0, **details):
    return observation(CALL, kind, at=BASE + at, details=details)


@pytest.fixture
def store(tmp_path):
    store = AuditStore(tmp_path / "audit.db")
    store.initialize()
    store.ingest(event("inbound_call_received", caller_number="+12025550123", called_number="+12025550124"))
    store.ingest(event("asterisk_answered", 0.2))
    return store


def wav_file(root, track="caller", seconds=2, silent=False):
    path = root / f"{CALL}-{track}.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes((b"\x00\x00" if silent else b"\x01\x02") * int(8000 * seconds))
    os.utime(path, (BASE, BASE))
    return path


def end_call(store, seconds=2.2):
    store.ingest(event("recording_started", 0.1))
    store.ingest(event("recording_stopped", seconds))
    store.ingest(event("channel_ended", seconds, disconnect_initiator="caller_hung_up"))


def provider_result(*utterances):
    return SimpleNamespace(name="test_transcriber", transcribe=Mock(return_value=list(utterances)))


def drain(publisher, store):
    while not publisher.queue.empty():
        store.ingest(publisher.queue.get_nowait())


@pytest.mark.parametrize("seconds", [0.1, 2, 3, 7, 9])
def test_short_call_without_ai_session_recovers_caller_words(store, tmp_path, seconds):
    for track in ("caller", "mixed", "sent"):
        wav_file(tmp_path, track, seconds)
    end_call(store, seconds + 0.2)
    transcriber = provider_result(Utterance("Hello? Is Alex", 0, round(seconds * 1000)))
    worker = MediaWorker(store, tmp_path, transcriber, True)
    worker.process(store.get(CALL))
    call = store.get(CALL)
    assert call["operator_zero_started"] == 0
    assert call["caller_spoke"] == 1
    assert call["recording_available"] == call["transcript_available"] == 1
    assert {r["track"] for r in call["recordings"]} == {"mixed", "caller", "sent"}
    assert call["transcript_segments"][0]["text"] == "Hello? Is Alex"
    assert call["transcript_segments"][0]["speaker"] == "caller"
    assert call["transcript_segments"][0]["start_offset_ms"] == 200
    assert all(not Path(r["path"]).is_absolute() for r in call["recordings"])
    worker.process(store.get(CALL))
    assert transcriber.transcribe.call_count == 1


def test_greeting_hangup_and_digital_silence_preserve_outcome(store, tmp_path):
    wav_file(tmp_path, silent=True)
    end_call(store)
    store.ingest(event("operator_zero_greeting_started", 0.5))
    provider = provider_result()
    MediaWorker(store, tmp_path, provider, True).process(store.get(CALL))
    call = store.get(CALL)
    assert call["outcome"] == "caller_hung_up_during_greeting"
    assert call["caller_spoke"] == 0
    provider.transcribe.assert_not_called()


def test_no_speech_call_classification(store, tmp_path):
    wav_file(tmp_path, silent=True)
    end_call(store)
    store.ingest(event("operator_zero_greeting_finished", 1))
    MediaWorker(store, tmp_path, provider_result(), True).process(store.get(CALL))
    assert store.get(CALL)["outcome"] == "no_speech"


def test_empty_recognition_on_nonzero_audio_is_not_proof_of_no_speech(store, tmp_path):
    wav_file(tmp_path)
    end_call(store)
    MediaWorker(store, tmp_path, provider_result(), True).process(store.get(CALL))
    assert store.get(CALL)["caller_spoke"] is None


def test_partial_text_survives_disconnect_and_late_final_replaces_it(store, tmp_path):
    publisher = AuditPublisher(tmp_path)
    publisher.known[CALL] = {}
    observer = RealtimeTranscriptObserver(SimpleNamespace(call_audit=publisher), CALL)
    observer.observe({"type": "conversation.item.input_audio_transcription.delta", "item_id": "caller-1", "delta": "Hello? Is"})
    drain(publisher, store)
    end_call(store)
    assert store.get(CALL)["transcript_segments"][0]["text"] == "Hello? Is"
    observer.observe({"type": "conversation.item.input_audio_transcription.completed", "item_id": "caller-1", "transcript": "Hello? Is Alex there?"})
    drain(publisher, store)
    call = store.get(CALL)
    assert len(call["transcript_segments"]) == 1
    assert call["transcript_segments"][0]["is_final"] == 1
    assert call["ended_at"] == timestamp(BASE + 2.2)


def test_assistant_partial_is_exact_generated_text_and_separate_from_caller(store, tmp_path):
    publisher = AuditPublisher(tmp_path)
    publisher.known[CALL] = {}
    observer = RealtimeTranscriptObserver(SimpleNamespace(call_audit=publisher), CALL)
    observer.observe({"type": "response.output_audio_transcript.delta", "item_id": "assistant", "delta": "Operator Zero. "})
    observer.observe({"type": "conversation.item.input_audio_transcription.completed", "item_id": "caller", "transcript": "Hello?"})
    observer.observe({"type": "response.output_audio_transcript.delta", "item_id": "assistant", "delta": "How may I help?"})
    observer.observe({"type": "response.output_audio_transcript.done", "item_id": "assistant", "transcript": "Operator Zero. How may I help?"})
    drain(publisher, store)
    segments = store.get(CALL)["transcript_segments"]
    assert len(segments) == 2
    assert {s["speaker"]: s["text"] for s in segments} == {"caller": "Hello?", "operator_zero": "Operator Zero. How may I help?"}
    assert all(s["confidence"] is None for s in segments)


def test_out_of_order_partial_cannot_overwrite_final(store):
    store.ingest(event("transcript_segment", 3, segment_id="item", speaker="caller", text="A few words", is_final=True, revision=2))
    store.ingest(event("transcript_segment", 4, segment_id="item", speaker="caller", text="A few", is_final=False, revision=1))
    assert store.get(CALL)["transcript_segments"][0]["text"] == "A few words"


def test_final_transcript_timings_repaired_by_late_start_metadata(tmp_path):
    store = AuditStore(tmp_path / "late.db")
    store.initialize()
    store.ingest(event("transcript_segment", 3, segment_id="item", speaker="caller", text="Hi", is_final=True,
                       start_at=timestamp(BASE + 1), end_at=timestamp(BASE + 2)))
    assert store.get(CALL)["transcript_segments"][0]["start_offset_ms"] is None
    store.ingest(event("inbound_call_received"))
    assert store.get(CALL)["transcript_segments"][0]["start_offset_ms"] == 1000


def test_recording_failure_does_not_erase_call_or_realtime_transcript(store, tmp_path):
    end_call(store)
    store.ingest(event("transcript_segment", 1, segment_id="item", speaker="caller", text="Hello", is_final=True))
    MediaWorker(store, tmp_path).process(store.get(CALL))
    call = store.get(CALL)
    assert call["transcript_available"] == 1 and call["recording_available"] == 0
    assert any(e["event_type"] == "recording_failed" for e in call["events"])


def test_transcription_failure_keeps_recording_and_retries(store, tmp_path):
    wav_file(tmp_path)
    end_call(store)
    transcriber = provider_result()
    transcriber.transcribe.side_effect = TimeoutError("provider timeout")
    worker = MediaWorker(store, tmp_path, transcriber, True)
    worker.process(store.get(CALL))
    assert store.get(CALL)["recording_available"] == 1
    assert any(e["event_type"] == "transcription_failed" for e in store.get(CALL)["events"])
    worker.process(store.get(CALL))
    assert transcriber.transcribe.call_count == 1  # Persisted retry backoff.
    with store.connect() as db:
        db.execute("UPDATE audit_jobs SET next_attempt_at=NULL")
    transcriber.transcribe.side_effect = None
    transcriber.transcribe.return_value = [Utterance("Hello", 0, 1000)]
    worker.process(store.get(CALL))
    assert store.get(CALL)["transcript_available"] == 1


def test_database_failure_stays_inside_independent_media_worker(store, tmp_path, monkeypatch):
    wav_file(tmp_path)
    end_call(store)
    worker = MediaWorker(store, tmp_path)
    monkeypatch.setattr(worker, "process", Mock(side_effect=sqlite3.OperationalError("disk full")))
    assert worker.scan() == 1
    assert store.get(CALL)["ended_at"] is not None


def test_live_call_recording_is_never_transcribed(store, tmp_path):
    wav_file(tmp_path)
    store.ingest(event("recording_started", 0.1))
    transcriber = provider_result()
    MediaWorker(store, tmp_path, transcriber, True).process(store.get(CALL))
    transcriber.transcribe.assert_not_called()


def test_unfinished_or_empty_wav_is_not_available(store, tmp_path):
    path = wav_file(tmp_path)
    path.write_bytes(path.read_bytes()[:48])
    os.utime(path, (BASE, BASE))
    end_call(store)
    MediaWorker(store, tmp_path).process(store.get(CALL))
    assert store.get(CALL)["recording_available"] == 0


def test_recording_path_rejects_traversal_and_symlink(tmp_path):
    with pytest.raises(ValueError):
        recording_path(tmp_path, "../secrets", "caller")
    path = tmp_path / f"{CALL}-caller.wav"
    path.symlink_to(tmp_path / "secret")
    with pytest.raises(ValueError):
        recording_path(tmp_path, CALL, "caller")


def test_fallback_reuses_final_caller_ranges_but_recovers_greeting_and_tail():
    call = {"transcript_segments": [{"speaker": "caller", "is_final": 1, "timing_source": "observed_speech_bounds",
                                    "start_offset_ms": 2000, "end_offset_ms": 4000}]}
    assert uncovered_intervals(call, 7000, 0) == [(0, 2250), (3750, 7000)]
    call["transcript_segments"][0]["is_final"] = 0
    assert uncovered_intervals(call, 7000, 0) == [(0, 7000)]


def test_identical_recovered_final_is_not_duplicated(store, tmp_path):
    wav_file(tmp_path)
    end_call(store)
    store.ingest(event("transcript_segment", 1, segment_id="item", speaker="caller", text="Hello?", is_final=True,
                       start_offset_ms=200, end_offset_ms=1000))
    MediaWorker(store, tmp_path, provider_result(Utterance("Hello", 0, 800)), True).process(store.get(CALL))
    assert len(store.get(CALL)["transcript_segments"]) == 1


@pytest.mark.parametrize("kind,event_type", [("OZ_RECORDING_STARTED", "recording_started"), ("OZ_RECORDING_STOPPED", "recording_stopped"), ("OZ_RECORDING_FAILED", "recording_failed")])
def test_native_recording_events(kind, event_type):
    events = normalize({"EventType": kind, "EventTime": BASE, "UniqueID": CALL,
                        "ChanName": "PJSIP/twilio-0001", "Context": "operator-zero-audit-recording-stop", "EventExtra": CALL}, known_call=True)
    assert events[0]["event_type"] == event_type


@pytest.mark.asyncio
async def test_real_provider_stop_preserves_already_received_partial_without_waiting(store, tmp_path, monkeypatch):
    from src.config import OpenAIRealtimeProviderConfig
    from src.providers.openai_realtime import OpenAIRealtimeProvider
    monkeypatch.setenv("CALL_TRANSCRIPTION_ENABLED", "true")
    publisher = AuditPublisher(tmp_path)
    publisher.known[CALL] = {}

    class Owner:
        call_audit = publisher
        async def on_event(self, event):
            pass

    owner = Owner()
    provider = OpenAIRealtimeProvider(OpenAIRealtimeProviderConfig(api_key="test-key"), owner.on_event)
    provider._call_id = CALL
    await provider._handle_event({"type": "conversation.item.input_audio_transcription.delta", "item_id": "short", "delta": "Is Alex"})
    await asyncio.wait_for(provider.stop_session(), 1)
    drain(publisher, store)
    assert store.get(CALL)["transcript_segments"][0]["text"] == "Is Alex"
    assert provider._call_id is None


@pytest.mark.asyncio
async def test_real_provider_audit_exception_does_not_break_transcript_delivery(monkeypatch):
    from src.config import OpenAIRealtimeProviderConfig
    from src.providers.openai_realtime import OpenAIRealtimeProvider
    monkeypatch.setenv("CALL_TRANSCRIPTION_ENABLED", "true")

    class Owner:
        call_audit = Mock()
        async def on_event(self, event):
            pass

    owner = Owner()
    owner.call_audit.emit.side_effect = OSError("disk unavailable")
    provider = OpenAIRealtimeProvider(OpenAIRealtimeProviderConfig(api_key="test-key"), owner.on_event)
    provider._call_id = CALL
    provider._emit_transcript = AsyncMock()
    await provider._handle_event({"type": "conversation.item.input_audio_transcription.completed", "item_id": "short", "transcript": "Hello"})
    provider._emit_transcript.assert_awaited_once_with("Hello", is_final=True)


def test_openai_adapter_uploads_only_pcm_wav_and_preserves_timing(monkeypatch):
    import openai
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)
    client.audio.transcriptions.create.return_value = SimpleNamespace(segments=[
        SimpleNamespace(text=" Hi ", start=0.1, end=0.7, no_speech_prob=0.01, avg_logprob=-0.2)])
    monkeypatch.setattr(openai, "OpenAI", Mock(return_value=client))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    result = OpenAIRecordingTranscriber().transcribe(b"\x01\x02" * 8000, 8000)
    assert result == [Utterance("Hi", 100, 700)]
    args = client.audio.transcriptions.create.call_args.kwargs
    assert args["model"] == "whisper-1" and args["file"][1].startswith(b"RIFF")


@pytest.mark.parametrize("kind,outcome", [("transfer_completed", "transferred_to_inside_phone"),
                                         ("hangup_accepted", "operator_zero_terminated"),
                                         ("call_completed", "completed")])
def test_media_enrichment_preserves_call_routing_outcome(store, tmp_path, kind, outcome):
    wav_file(tmp_path)
    store.ingest(event(kind, 1))
    end_call(store)
    MediaWorker(store, tmp_path, provider_result(Utterance("Hi", 0, 500)), True).process(store.get(CALL))
    assert store.get(CALL)["outcome"] == outcome


def test_chunk_retry_does_not_repeat_completed_upload_or_transcript(store, tmp_path):
    wav_file(tmp_path, seconds=301)
    end_call(store, 301.2)
    transcriber = provider_result()
    transcriber.transcribe.side_effect = [[Utterance("First words", 0, 500)], TimeoutError()]
    worker = MediaWorker(store, tmp_path, transcriber, True)
    worker.process(store.get(CALL))
    assert len(store.get(CALL)["transcript_segments"]) == 1
    with store.connect() as db:
        db.execute("UPDATE audit_jobs SET next_attempt_at=NULL")
    transcriber.transcribe.side_effect = [[Utterance("Last words", 0, 500)]]
    worker.process(store.get(CALL))
    assert transcriber.transcribe.call_count == 3
    assert {s["text"] for s in store.get(CALL)["transcript_segments"]} == {"First words", "Last words"}


def test_abnormal_end_without_stop_marker_recovers_stable_recording(store, tmp_path):
    wav_file(tmp_path)
    store.ingest(event("recording_started", 0.1))
    store.ingest(event("channel_ended", 2.2, hangup_cause=102, disconnect_initiator="network_failure"))
    MediaWorker(store, tmp_path, provider_result(Utterance("Hello", 0, 1000)), True).process(store.get(CALL))
    call = store.get(CALL)
    assert call["outcome"] == "failed" and call["disconnect_initiator"] == "network_failure"
    assert call["recording_available"] == call["transcript_available"] == 1


def test_transcription_disabled_does_not_publish_text_but_keeps_greeting_events(tmp_path, monkeypatch):
    from src.core.call_audit.realtime import observe
    monkeypatch.setenv("CALL_TRANSCRIPTION_ENABLED", "false")
    publisher = AuditPublisher(tmp_path)
    publisher.known[CALL] = {}

    class Owner:
        call_audit = publisher
        async def on_event(self, event):
            pass

    provider = SimpleNamespace(on_event=Owner().on_event, _call_id=CALL)
    observe(provider, {"type": "response.audio.delta", "delta": "audio"})
    observe(provider, {"type": "conversation.item.input_audio_transcription.completed", "transcript": "Private words"})
    assert publisher.queue.qsize() == 1
    assert publisher.queue.get_nowait()["event_type"] == "operator_zero_greeting_started"


def test_journal_replay_recovers_transcript_after_engine_process_is_gone(store, tmp_path):
    publisher = AuditPublisher(tmp_path / "journals")
    publisher.known[CALL] = {}
    publisher.start()
    observer = RealtimeTranscriptObserver(SimpleNamespace(call_audit=publisher), CALL)
    observer.observe({"type": "conversation.item.input_audio_transcription.delta", "item_id": "short", "delta": "Hello? Is"})
    publisher.close()
    end_call(store)
    assert scan(store, tmp_path / "journals") == 0
    assert store.get(CALL)["transcript_segments"][0]["text"] == "Hello? Is"
    assert scan(store, tmp_path / "journals") == 0
    assert len(store.get(CALL)["transcript_segments"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("drained", [True, False])
async def test_greeting_finish_requires_confirmed_transport_drain(drained):
    from src.engine import Engine
    engine = Engine.__new__(Engine)
    engine.call_audit = Mock()
    engine._wait_for_call_audio_drain = AsyncMock(return_value=drained)
    engine._terminal_transport_quiet_sec = Mock(return_value=0.1)
    engine._provider_output_drain_tasks = {CALL: asyncio.current_task()}
    engine._agent_output_active_calls = {CALL}
    engine.session_store = SimpleNamespace(get_by_call_id=AsyncMock(return_value=SimpleNamespace(cleanup_in_progress=False)))
    engine._clear_tts_gating_after_provider_drain = AsyncMock()
    await engine._finish_provider_output_after_drain(CALL, reset_timer=False, preserve_policy_state=True, clear_tts_gating_after_drain=True)
    engine._clear_tts_gating_after_provider_drain.assert_awaited_once()
    if drained:
        engine.call_audit.emit.assert_called_once_with(CALL, "operator_zero_greeting_finished", evidence="caller_transport_drained")
    else:
        engine.call_audit.emit.assert_not_called()
