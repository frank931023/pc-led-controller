"""
Tests for the PC control center. No Pi or Beszel hub needed: pi_client is replaced
by fakes and the background threads are not started.

Run:  pip install -r requirements.txt pytest && python -m pytest
"""
import json

import pytest
import requests

import app as pc
import pi_client
from devices import DeviceStore

DEVICE = {'host': 'pi-01.test', 'port': 5000, 'name': 'pi-01'}


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(pc, 'store', DeviceStore(str(tmp_path / 'pi_config.json'), {}))
    monitor = pc.StatusMonitor(pc.store)
    monkeypatch.setattr(monitor, 'start', lambda: None)
    monkeypatch.setattr(monitor, 'check_soon', lambda device_id: None)
    monkeypatch.setattr(pc, 'monitor', monitor)
    monkeypatch.setattr(pc, 'API_TOKEN', '')
    pc.app.config['TESTING'] = True
    return pc.app.test_client()


@pytest.fixture
def sent(monkeypatch):
    """Records commands sent to Pis and answers DONE."""
    calls = []

    def fake_send(device, command, timeout):
        calls.append({'command': command, 'timeout': timeout})
        return {'arduino_result': 'DONE', 'arduino_response': ['DONE']}

    monkeypatch.setattr(pi_client, 'send_command', fake_send)
    return calls


# ----- device list -----

def test_upsert_keeps_ui_fields(tmp_path):
    store = DeviceStore(str(tmp_path / 'c.json'), {})
    device, created = store.upsert('pi-01', DEVICE)
    assert created and device['name'] == 'pi-01'
    store.update('pi-01', alternative_name='Left rail', beszel_system_id='abc')

    device, created = store.upsert('pi-01', {'host': '10.0.0.9', 'port': 5000})
    assert not created
    assert device['host'] == '10.0.0.9'
    assert device['alternative_name'] == 'Left rail'
    assert device['beszel_system_id'] == 'abc'
    assert json.loads((tmp_path / 'c.json').read_text())['pis']['pi-01']['host'] == '10.0.0.9'


def test_put_creates_then_updates(client):
    first = client.put('/api/pi/pi-01', json=DEVICE)
    assert first.status_code == 201 and first.get_json()['created']
    client.patch('/api/pi/pi-01', json={'alternative_name': 'Left rail'})

    again = client.put('/api/pi/pi-01', json={**DEVICE, 'host': '10.0.0.9'})
    assert again.status_code == 200
    pi = again.get_json()['pi']
    assert (pi['host'], pi['alternative_name']) == ('10.0.0.9', 'Left rail')


@pytest.mark.parametrize('pi_id, body', [
    ('bad id!', DEVICE),
    ('pi-01', {**DEVICE, 'host': 'http://x'}),
    ('pi-01', {**DEVICE, 'port': 70000}),
])
def test_put_validation(client, pi_id, body):
    assert client.put(f'/api/pi/{pi_id}', json=body).status_code in (400, 404)


def test_starts_with_no_devices(client):
    assert pc.DEFAULT_DEVICES == {}
    assert client.get('/api/pis').get_json()['pis'] == {}


# ----- timeouts -----

def test_command_without_timeout_uses_the_pis_default(client, sent):
    client.put('/api/pi/pi-01', json=DEVICE)
    pc.monitor._apply('pi-01', pi_client.Probe('online', {'command_timeout': 130, 'max_command_timeout': 300}), 0)
    assert client.post('/api/pi/pi-01/command', json={'command': 'SXC'}).status_code == 200
    assert sent == [{'command': 'SXC', 'timeout': 130}]


def test_command_without_timeout_before_first_check_uses_pc_default(client, sent):
    client.put('/api/pi/pi-01', json=DEVICE)
    client.post('/api/pi/pi-01/command', json={'command': 'SXC'})
    assert sent[0]['timeout'] == pc.DEFAULT_COMMAND_TIMEOUT


def test_timeout_limit_follows_the_pi(client, sent):
    client.put('/api/pi/pi-01', json=DEVICE)
    pc.monitor._apply('pi-01', pi_client.Probe('online', {'command_timeout': 130, 'max_command_timeout': 200}), 0)
    assert client.post('/api/pi/pi-01/command', json={'command': 'SXC', 'timeout': 200}).status_code == 200
    resp = client.post('/api/pi/pi-01/command', json={'command': 'SXC', 'timeout': 250})
    assert resp.status_code == 400 and '200' in resp.get_json()['error']


def test_http_wait_is_longer_than_the_pi_timeout(monkeypatch):
    seen = {}

    def fake_request(method, url, timeout, **kwargs):
        seen.update(timeout=timeout, body=kwargs['json'])
        raise requests.exceptions.ConnectionError()

    monkeypatch.setattr(requests, 'request', fake_request)
    with pytest.raises(pi_client.PiError):
        pi_client.send_command(DEVICE, 'SXC', 130)
    assert seen['body'] == {'command': 'SXC', 'timeout': 130}
    assert seen['timeout'] > 130


# ----- busy -----

class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code, self._body, self.text = status_code, body, json.dumps(body)

    def json(self):
        return self._body


def test_pi_409_is_reported_as_busy(monkeypatch):
    monkeypatch.setattr(requests, 'request', lambda *a, **k: FakeResponse(
        409, {'status': 'error', 'error': 'BUSY', 'running': {'command': 'SXC', 'seconds': 3}}))
    with pytest.raises(pi_client.PiError) as e:
        pi_client.send_command(DEVICE, 'SYC', 130)
    assert (e.value.pi_status, e.value.http_status) == (pi_client.BUSY, 409)
    assert 'SXC' in str(e.value)


def test_probe_reads_busy_and_info(monkeypatch):
    monkeypatch.setattr(requests, 'request', lambda *a, **k: FakeResponse(200, {
        'status': 'success', 'arduino_status': 'busy', 'running': {'command': 'SXC', 'seconds': 3},
        'command_timeout': 130, 'max_command_timeout': 300, 'app_version': 'main@abc1234'}))
    probe = pi_client.probe(DEVICE)
    assert probe.status == pi_client.BUSY
    assert probe.info['app_version'] == 'main@abc1234'


def test_remote_command_shows_as_running(client):
    client.put('/api/pi/pi-01', json=DEVICE)
    pc.monitor._apply('pi-01', pi_client.Probe('busy', {'running': {'command': 'SXC', 'seconds': 3}}), 0)
    pi = client.get('/api/pis').get_json()['pis']['pi-01']
    assert (pi['status'], pi['running']) == ('busy', 'SXC')

    pc.monitor.set('pi-01', pi_client.ONLINE)       # finished
    assert client.get('/api/pis').get_json()['pis']['pi-01']['running'] is None


# ----- catalog -----

def test_command_catalog_is_proxied(client, monkeypatch):
    client.put('/api/pi/pi-01', json=DEVICE)
    monkeypatch.setattr(pi_client, 'commands', lambda device: {
        'status': 'success', 'groups': [], 'home_sequence': ['SXH'], 'default_timeout': 130})
    body = client.get('/api/pi/pi-01/commands').get_json()
    assert body['home_sequence'] == ['SXH'] and body['default_timeout'] == 130


# ----- health history -----

STATS_RECORD = {'created': '2026-01-02 03:04:05.678Z',
                'stats': {'cpu': 12.5, 'mp': 40.1, 'dp': 7.7, 't': {'cpu_thermal': 51.2, 'rp1': 49.0},
                          'la': [0.42, 0.3, 0.2]}}


@pytest.fixture
def beszel(monkeypatch):
    """A configured Beszel client that knows one system, 'sys1', and records requests."""
    calls = []
    hub = pc.BeszelClient('http://hub.test', 'a@b.c', 'pw')

    def fake_request(method, path, **kwargs):
        calls.append(kwargs.get('params'))
        return {'items': [STATS_RECORD, {'created': '2026-01-02 03:05:05.678Z', 'stats': {'cpu': 3}}]}

    monkeypatch.setattr(hub, '_request', fake_request)
    sync = pc.BeszelSync(hub, pc.store)
    monkeypatch.setattr(sync, 'start', lambda: None)
    monkeypatch.setattr(sync, '_systems', {'sys1': {'id': 'sys1'}})
    monkeypatch.setattr(pc, 'health', sync)
    return calls


def test_health_history_is_converted_for_the_charts(client, beszel):
    client.put('/api/pi/pi-01', json=DEVICE)
    pc.store.update('pi-01', beszel_system_id='sys1')

    body = client.get('/api/pi/pi-01/health/history?range=24h').get_json()
    assert body['range'] == '24h' and body['interval'] == 1200 and body['to'] - body['from'] == 86400
    assert '1h' in body['ranges'] and '30d' in body['ranges']
    first, second = body['points']
    assert first == {'t': 1767323045, 'cpu': 12.5, 'mem': 40.1, 'disk': 7.7, 'temp': 51.2, 'load': 0.42}
    assert second['cpu'] == 3 and second['temp'] is None and second['load'] is None
    assert "system='sys1' && type='20m'" in beszel[0]['filter']


def test_health_history_errors(client, beszel):
    client.put('/api/pi/pi-01', json=DEVICE)
    assert client.get('/api/pi/nope/health/history').status_code == 404
    assert client.get('/api/pi/pi-01/health/history?range=5y').status_code == 400
    assert client.get('/api/pi/pi-01/health/history').get_json()['error'] == 'Not linked to a monitoring agent'


# ----- API token -----

def test_token_protects_writes_only(client, monkeypatch):
    monkeypatch.setattr(pc, 'API_TOKEN', 's3cret')
    assert client.get('/api/pis').status_code == 200
    assert client.put('/api/pi/pi-01', json=DEVICE).status_code == 401
    assert client.put('/api/pi/pi-01', json=DEVICE, headers={'Authorization': 'Bearer nope'}).status_code == 401
    ok = client.put('/api/pi/pi-01', json=DEVICE, headers={'Authorization': 'Bearer s3cret'})
    assert ok.status_code == 201
    assert client.get('/api/beszel/agent-env').status_code == 401
    assert client.get('/api/health').get_json()['auth_required'] is True
