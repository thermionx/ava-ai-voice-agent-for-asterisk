"""Exercise real authentication and read-only audit routes with synthetic calls."""
import importlib.util
import os
from pathlib import Path
import sys
import wave

import httpx
import pytest

if importlib.util.find_spec('fastapi') is None:
    pytest.skip('Admin UI dependencies are not installed', allow_module_level=True)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'admin_ui' / 'backend'))
import auth
from api import call_audit
from fastapi import FastAPI
from src.core.call_audit.events import observation
from src.core.call_audit.store import AuditStore

CALL = '1700000000.123'
BASE = 1700000000


@pytest.fixture
def audit_api(tmp_path, monkeypatch):
    monkeypatch.setenv('CALL_AUDIT_DB_PATH', str(tmp_path / 'audit.db'))
    monkeypatch.setenv('CALL_RECORDING_DIR', str(tmp_path))
    monkeypatch.setenv('CALL_AUDIT_ENABLED', 'true')
    monkeypatch.setattr(auth, 'SECRET_KEY', 'synthetic-test-secret-only')
    monkeypatch.setattr(auth, 'get_user', lambda name: auth.UserInDB(username=name, hashed_password='unused') if name == 'admin' else None)
    store = AuditStore(tmp_path / 'audit.db')
    store.initialize()
    store.ingest(observation(CALL, 'inbound_call_received', at=BASE, details={'caller_number': '+12025550123', 'called_number': '+12025550124', 'caller_name': 'Example Caller'}))
    store.ingest(observation(CALL, 'asterisk_answered', at=BASE + 0.1))
    store.ingest(observation(CALL, 'channel_ended', at=BASE + 2, details={'disconnect_initiator': 'caller_hung_up', 'hangup_cause': 16}))
    store.ingest(observation(CALL, 'transcript_segment', at=BASE + 1, details={'speaker': 'caller', 'text': 'Hello? Is Alex', 'is_final': False, 'segment_id': 'item', 'start_offset_ms': 200, 'end_offset_ms': 1600, 'recording_path': '/private/secret.wav'}))
    app = FastAPI()
    app.include_router(call_audit.router, prefix='/api')
    token = auth.create_access_token({'sub': 'admin'})
    return app, store, {'Authorization': f'Bearer {token}'}, tmp_path


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['/status', '/calls', f'/calls/{CALL}', f'/calls/{CALL}/recordings/caller'])
async def test_authentication_required_for_all_audit_routes(audit_api, path):
    app, _, _, _ = audit_api
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        assert (await client.get('/api/call-audit' + path)).status_code == 401
        assert (await client.get('/api/call-audit' + path, headers={'Authorization': 'Bearer invalid'})).status_code == 401


@pytest.mark.asyncio
async def test_pending_password_change_cannot_read_audit(audit_api, monkeypatch):
    app, _, headers, _ = audit_api
    monkeypatch.setattr(auth, 'get_user', lambda name: auth.UserInDB(username=name, hashed_password='unused', must_change_password=True))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        assert (await client.get('/api/call-audit/calls', headers=headers)).status_code == 403


@pytest.mark.asyncio
async def test_short_call_listing_and_detail_without_ai_session(audit_api):
    app, _, headers, _ = audit_api
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test', headers=headers) as client:
        response = await client.get('/api/call-audit/calls')
        assert response.status_code == 200
        assert response.headers['cache-control'] == 'private, no-store'
        row = response.json()['calls'][0]
        assert row['duration_seconds'] == 2 and row['operator_zero_started'] is False
        assert row['called_number'] == '+12025550124'
        assert row['transcript_preview']['text'] == 'Hello? Is Alex'
        detail = await client.get(f'/api/call-audit/calls/{CALL}')
        assert detail.json()['transcript_segments'][0]['speaker'] == 'caller'
        assert detail.json()['disconnect_initiator'] == 'caller_hung_up'
        assert '/private/' not in detail.text and 'recording_path' not in detail.text
        assert len(detail.json()['events']) == 4


@pytest.mark.asyncio
async def test_search_outcome_pagination_and_newest_first(audit_api):
    app, store, headers, _ = audit_api
    store.ingest(observation('1700000001.124', 'inbound_call_received', at=BASE + 50))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test', headers=headers) as client:
        data = (await client.get('/api/call-audit/calls?page_size=1')).json()
        assert data['calls'][0]['call_id'] == '1700000001.124' and data['total_pages'] == 2
        assert (await client.get('/api/call-audit/calls?page=2&page_size=1')).json()['calls'][0]['call_id'] == CALL
        for query in ['Alex', 'Example Caller', '+12025550124']:
            assert (await client.get('/api/call-audit/calls', params={'search': query})).json()['total'] == 1
        assert (await client.get('/api/call-audit/calls', params={'search': "%' OR 1=1 --"})).json()['total'] == 0
        assert (await client.get('/api/call-audit/calls?outcome=in_progress')).json()['total'] == 1
        assert (await client.get('/api/call-audit/calls?page_size=1000')).status_code == 422
        assert (await client.get('/api/call-audit/calls/no-such-call')).status_code == 404


@pytest.mark.asyncio
async def test_missing_database_returns_503_without_creating_it(audit_api, monkeypatch):
    app, _, headers, root = audit_api
    path = root / 'missing.db'
    monkeypatch.setenv('CALL_AUDIT_DB_PATH', str(path))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test', headers=headers) as client:
        assert (await client.get('/api/call-audit/calls')).status_code == 503
        assert not path.exists()


def make_recording(store, root):
    path = root / f'{CALL}-caller.wav'
    with wave.open(str(path), 'wb') as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(8000)
        wav.writeframes(b'\x01\x02' * 800)
    with store.connect() as db:
        db.execute('INSERT INTO call_recordings VALUES (?,?,?,?,?,?)', ('rec', CALL, 'caller', path.name, 'available', '2023-11-14'))
    return path


@pytest.mark.asyncio
async def test_authenticated_recording_stream_ranges_and_no_filesystem_paths(audit_api):
    app, store, headers, root = audit_api
    path = make_recording(store, root)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test', headers=headers) as client:
        url = f'/api/call-audit/calls/{CALL}/recordings/caller'
        full = await client.get(url)
        assert full.status_code == 200 and full.content == path.read_bytes()
        assert full.headers['cache-control'] == 'private, no-store'
        assert str(root) not in str(full.headers)
        part = await client.get(url, headers={'Range': 'bytes=0-3'})
        assert part.status_code == 206 and part.content == b'RIFF'
        suffix = await client.get(url, headers={'Range': 'bytes=-4'})
        assert suffix.content == path.read_bytes()[-4:]
        assert (await client.get(url, headers={'Range': 'bytes=99999-'})).status_code == 416
        assert (await client.get(url, headers={'Range': 'bytes=0-1,3-4'})).status_code == 416
        assert (await client.get(url + 'bad')).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize('attack', ['symlink', 'db_path', 'missing', 'fifo'])
async def test_recording_rejects_unsafe_or_missing_files(audit_api, attack):
    app, store, headers, root = audit_api
    path = make_recording(store, root)
    if attack == 'db_path':
        with store.connect() as db:
            db.execute("UPDATE call_recordings SET path='../secret.wav'")
    else:
        path.unlink()
        if attack == 'symlink':
            secret = root / 'secret'
            secret.write_bytes(b'private' * 100)
            path.symlink_to(secret)
        elif attack == 'fifo':
            os.mkfifo(path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test', headers=headers) as client:
        assert (await client.get(f'/api/call-audit/calls/{CALL}/recordings/caller')).status_code == 404


@pytest.mark.asyncio
async def test_api_read_does_not_mutate_audit_database(audit_api):
    app, store, headers, _ = audit_api
    with store.connect() as db:
        before = list(db.iterdump())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test', headers=headers) as client:
        await client.get('/api/call-audit/calls')
        await client.get(f'/api/call-audit/calls/{CALL}')
    with store.connect() as db:
        assert list(db.iterdump()) == before


@pytest.mark.asyncio
async def test_existing_recording_info_no_longer_exposes_host_path(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from api import calls
    recording = tmp_path / 'example.wav'
    recording.write_bytes(b'RIFF' + b'\x00' * 100)
    store = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(call_id=CALL, start_time=None)))
    monkeypatch.setattr(calls, '_get_call_history_store', lambda: store)
    monkeypatch.setattr(calls, '_find_recording', lambda *args: recording)
    result = await calls.get_call_recording_info('record-id')
    assert result.has_recording and result.file_path is None
    assert str(tmp_path) not in result.model_dump_json()
