import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from api import trusted_callers as api


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv('OPERATOR_ZERO_MEMORY_ADMIN_TOKEN', 'test-admin-only')
    monkeypatch.setenv('OPERATOR_ZERO_PHONEBOOK_PASSWORD', 'test-directory-only')
    monkeypatch.setenv('OPERATOR_ZERO_PHONEBOOK_URL', 'http://mini-pc:8790/phonebook/phonebook.xml')
    app = FastAPI()
    app.include_router(api.router, prefix='/api')
    app.dependency_overrides[api.get_current_user] = lambda: {'username': 'household'}
    return TestClient(app)


def test_router_requires_authentication():
    app = FastAPI()
    app.include_router(api.router, prefix='/api')
    client = TestClient(app)
    for method, path in [('GET',''), ('POST',''), ('DELETE','?phone=2025550100'), ('GET','/phonebook'), ('GET','/phonebook/password')]:
        assert client.request(method, '/api/trusted-callers'+path).status_code in (401, 403)


def test_crud_proxy_uses_fixed_service_and_private_bearer(client, monkeypatch):
    requests = []
    async def handle(request):
        requests.append(request)
        return httpx.Response(200, json={'count': 0, 'callers': []})
    original = httpx.AsyncClient
    monkeypatch.setattr(api.httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    for response in [client.get('/api/trusted-callers'), client.post('/api/trusted-callers', json={'caller_number':'2025550100', 'caller_name':'Alice'}), client.delete('/api/trusted-callers?phone=%2B12025550100')]:
        assert response.status_code == 200
        assert response.headers['cache-control'] == 'no-store'
    assert [r.method for r in requests] == ['GET', 'POST', 'DELETE']
    assert all(r.url.host == '127.0.0.1' for r in requests)
    assert all(r.headers['authorization'] == 'Bearer test-admin-only' for r in requests)
    assert requests[-1].url.params['phone'] == '+12025550100'


@pytest.mark.parametrize('status', [401, 500, 302])
def test_upstream_errors_do_not_leak_secrets_or_look_like_empty_directory(client, monkeypatch, status):
    async def handle(request):
        return httpx.Response(status, text='private upstream credentials')
    original = httpx.AsyncClient
    monkeypatch.setattr(api.httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    response = client.get('/api/trusted-callers')
    assert response.status_code == 503
    assert 'private upstream credentials' not in response.text


def test_missing_integration_and_explicit_password_reveal(client, monkeypatch):
    response = client.get('/api/trusted-callers/phonebook')
    assert response.json()['enabled'] is True
    assert 'test-directory-only' not in response.text
    response = client.get('/api/trusted-callers/phonebook/password')
    assert response.json() == {'password':'test-directory-only'}
    assert response.headers['cache-control'] == 'no-store'
    monkeypatch.delenv('OPERATOR_ZERO_MEMORY_ADMIN_TOKEN')
    assert client.get('/api/trusted-callers').status_code == 503
    monkeypatch.delenv('OPERATOR_ZERO_PHONEBOOK_PASSWORD')
    assert client.get('/api/trusted-callers/phonebook').json()['enabled'] is False
    assert client.get('/api/trusted-callers/phonebook/password').status_code == 503


def test_duplicate_add_is_forwarded_without_overwriting(client, monkeypatch):
    import json
    requests = []
    async def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(400, json={'error': 'This phone number is already in use. Edit the existing caller instead.'})
    original = httpx.AsyncClient
    monkeypatch.setattr(api.httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    response = client.post('/api/trusted-callers', json={'caller_number': '2025550100'})
    assert response.status_code == 400
    assert 'already in use' in response.json()['detail']
    assert requests[-1]['create_only'] is True
    client.post('/api/trusted-callers', json={'caller_number': '2025550100', 'create_only': False})
    assert requests[-1]['create_only'] is False
