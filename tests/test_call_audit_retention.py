from datetime import datetime, timedelta, timezone
import json
import os
import pytest

from src.core.call_audit.events import observation, timestamp
from src.core.call_audit.maintenance import maintenance_lock
from src.core.call_audit.retention import cleanup
from src.core.call_audit.store import AuditStore
from src.core.call_audit.worker import ingest_file

NOW = datetime(2026, 9, 8, tzinfo=timezone.utc)
CALL = '1700000000.123'


@pytest.fixture
def store(tmp_path):
    result = AuditStore(tmp_path / 'audit.db')
    result.initialize()
    return result


def seed(store, age, ended=True):
    start = NOW - timedelta(days=age)
    initial = observation(CALL, 'inbound_call_received', at=start)
    store.ingest(initial)
    if ended:
        store.ingest(observation(CALL, 'channel_ended', at=start + timedelta(seconds=2)))
    return initial


def test_dry_run_changes_nothing(store, tmp_path):
    seed(store, 400)
    recording = tmp_path / f'{CALL}-mixed.wav'
    recording.write_bytes(b'original audio')
    result = cleanup(store, tmp_path, now=NOW)
    assert result['metadata_call_ids'] == [CALL]
    assert result['recording_files'] == [recording.name]
    assert store.get(CALL) and recording.exists()


def test_recording_expiry_preserves_metadata_and_stops_media_retries(store, tmp_path):
    seed(store, 40)
    recording = tmp_path / f'{CALL}-caller.wav'
    recording.write_bytes(b'audio')
    cleanup(store, tmp_path, now=NOW, apply=True)
    assert not recording.exists() and store.get(CALL)
    with store.connect() as db:
        assert db.execute('SELECT status FROM audit_jobs WHERE id=?', (f'transcription:{CALL}',)).fetchone()[0] == 'expired'


def test_metadata_expiry_tombstone_prevents_replay_resurrection(store, tmp_path):
    initial = seed(store, 400)
    cleanup(store, tmp_path, now=NOW, apply=True)
    assert store.get(CALL) is None
    store.ingest(initial)
    store.ingest(observation(CALL, 'transcript_segment', details={'speaker': 'caller', 'segment_id': 'late', 'text': 'late words'}))
    assert store.get(CALL) is None


def test_active_call_is_never_expired(store, tmp_path):
    seed(store, 400, ended=False)
    result = cleanup(store, tmp_path, now=NOW, apply=True)
    assert result['recording_call_ids'] == result['metadata_call_ids'] == []
    assert store.get(CALL)


@pytest.mark.parametrize('recording_days,metadata_days', [(-1, 365), (30, 10), (0, 365)])
def test_invalid_retention_policy_refuses_deletion(store, tmp_path, recording_days, metadata_days):
    seed(store, 400)
    with pytest.raises(ValueError):
        cleanup(store, tmp_path, apply=True, now=NOW, recording_days=recording_days, metadata_days=metadata_days)
    assert store.get(CALL)


def test_zero_disables_retention(store, tmp_path):
    seed(store, 400)
    result = cleanup(store, tmp_path, now=NOW, apply=True, recording_days=0, metadata_days=0)
    assert result['recording_call_ids'] == result['metadata_call_ids'] == []


def test_symlink_recording_prevents_deletion(store, tmp_path):
    seed(store, 400)
    secret = tmp_path / 'secret'
    secret.write_text('private')
    (tmp_path / f'{CALL}-caller.wav').symlink_to(secret)
    with pytest.raises(ValueError):
        cleanup(store, tmp_path, now=NOW, apply=True)
    assert secret.read_text() == 'private' and store.get(CALL)


def test_busy_worker_blocks_apply(store, tmp_path):
    seed(store, 400)
    with maintenance_lock(store):
        with pytest.raises(BlockingIOError):
            cleanup(store, tmp_path, now=NOW, apply=True)
    assert store.get(CALL)


def test_only_ingested_closed_old_journals_are_expired(store, tmp_path):
    initial = seed(store, 400)
    journals = tmp_path / 'journals'
    journals.mkdir()
    old = journals / 'engine-test-20200101.jsonl'
    old.write_text(json.dumps(initial) + '\n')
    current = journals / 'engine-test-20260908.jsonl'
    current.write_text(json.dumps(initial) + '\n')
    assert cleanup(store, tmp_path, now=NOW, journal_dir=journals)['journal_archives'] == []
    ingest_file(store, old, 'engine')
    result = cleanup(store, tmp_path, now=NOW, journal_dir=journals, apply=True)
    assert result['journal_archives'] == [old.name]
    assert not old.exists() and current.exists()


def test_malformed_closed_journal_is_retained(store, tmp_path):
    seed(store, 400)
    journals = tmp_path / 'journals'
    journals.mkdir()
    old = journals / 'engine-test-20200101.jsonl'
    old.write_text('{truncated')
    assert cleanup(store, tmp_path, now=NOW, journal_dir=journals)['journal_archives'] == []
