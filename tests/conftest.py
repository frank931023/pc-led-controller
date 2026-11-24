import json
import os
import tempfile
import time

import pytest

# app.py creates the app (and its DeviceStore) at import time: point it at a throw-away file first,
# and keep Beszel off so nothing tries to reach a hub.
os.environ['CONFIG_FILE'] = os.path.join(tempfile.mkdtemp(), 'pi_config.json')
os.environ['BESZEL_URL'] = ''

import app as entry                                                     # noqa: E402
from mcc import pi_client, services                                     # noqa: E402
from mcc.devices import DeviceStore                                     # noqa: E402
from mcc.monitor import StatusMonitor                                   # noqa: E402
from mcc.room import Room, RoomConfig, RoomState, Simulation            # noqa: E402
from mcc.scripts import SCRIPTS, ScriptRunner                           # noqa: E402
from mcc.setups import SetupStore                                       # noqa: E402

# A small room: two Pis, every kind of axis, plus one axis planned but not verified on the
# rig and one not wired at all. Speeds are high so every move takes milliseconds.
AXES = {'axes': [
    {'id': 'a.lift', 'device': 'pi-01', 'ch': 'SY', 'min': 0.5, 'max': 1.5, 'unit': 'm', 'speed': 10, 'verified': True},
    {'id': 'a.x', 'device': 'pi-01', 'ch': 'SX', 'min': -1.0, 'max': 1.0, 'unit': 'm', 'speed': 10, 'verified': True},
    {'id': 'a.tilt', 'device': 'pi-01', 'ch': 'T', 'min': -15, 'max': 35, 'unit': 'deg', 'speed': 100, 'verified': True},
    {'id': 'b.z', 'device': 'pi-02', 'ch': 'SZ', 'min': 0.0, 'max': 2.0, 'unit': 'm', 'speed': 10, 'verified': True},
    {'id': 'b.lift', 'device': 'pi-02', 'ch': 'SY', 'min': 0.0, 'max': 1.0, 'unit': 'm', 'speed': 10},
    {'id': 'c.x', 'device': None, 'ch': None, 'min': 0.0, 'max': 1.0, 'unit': 'm', 'speed': 10},
]}

LIBRARY = [
    {'id': 'ex-01', 'name': 'Example', 'group': 'Demo', 'axes': {'a.lift': 0.8}},
    {'id': 'ex-02', 'name': 'Example 2', 'group': 'Demo', 'axes': {'a.x': 0.5, 'b.z': 1.2}},
]


def short_sleep(seconds):
    """Simulated moves take real time, but never more than 20 ms: tests stay fast and a
    job is still "running" long enough to be caught halfway."""
    time.sleep(min(seconds, 0.02))


class FakePis:
    """Answers like the Pi: DONE, plus the Arduino session of each device."""

    def __init__(self):
        self.sent = []                       # (device, command)
        self.session = {'pi-01': 's1', 'pi-02': 's1'}
        self.extra = {}                      # command -> extra response lines
        self.restart_on = None               # command that makes the Arduino restart

    def send(self, device, command, timeout):
        device_id = device['name']
        self.sent.append((device_id, command))
        if command == self.restart_on:
            self.session[device_id] += 'r'
            self.restart_on = None
        lines = self.extra.get(command, []) + ['DONE']
        return {'arduino_result': 'DONE', 'arduino_response': lines,
                'arduino_session': self.session[device_id]}

    def commands(self, device_id=None):
        return [c for d, c in self.sent if device_id in (None, d)]


@pytest.fixture
def rig(monkeypatch, tmp_path):
    """The control center with two fake Pis online, a small axis table, two example
    setups and a test client (rig.client). rig.sent lists every command sent."""
    (tmp_path / 'room.json').write_text(json.dumps(AXES), encoding='utf-8')
    store = DeviceStore(str(tmp_path / 'pi_config.json'), {})
    for device_id in ('pi-01', 'pi-02'):
        store.upsert(device_id, {'host': f'{device_id}.test', 'port': 5000, 'name': device_id})
    monitor = StatusMonitor(store)
    monkeypatch.setattr(monitor, 'start', lambda: None)
    monkeypatch.setattr(monitor, 'check_soon', lambda device_id: None)
    for device_id in ('pi-01', 'pi-02'):
        monitor.set(device_id, pi_client.ONLINE)
    fake = FakePis()
    monkeypatch.setattr(monitor, 'info', lambda d: {'arduino_session': fake.session.get(d), 'command_timeout': 30})
    monkeypatch.setattr(pi_client, 'send_command', fake.send)
    monkeypatch.setattr(services, 'store', store)
    monkeypatch.setattr(services, 'monitor', monitor)
    monkeypatch.setattr(services, 'room', Room(RoomConfig([str(tmp_path / 'room.json')]),
                                               RoomState(str(tmp_path / 'room_state.json')),
                                               Simulation(str(tmp_path / 'simulation.json')),
                                               sleep=short_sleep))
    library = tmp_path / 'library'
    library.mkdir()
    for setup in LIBRARY:
        (library / f'{setup["id"]}.json').write_text(json.dumps(setup), encoding='utf-8')
    monkeypatch.setattr(services, 'setups', SetupStore(str(tmp_path / 'setups'), str(library)))
    monkeypatch.setattr(services, 'scripts', ScriptRunner(SCRIPTS, sleep=lambda s: time.sleep(min(s, 0.002)),
                                                          poll_interval=0.005))
    monkeypatch.setattr(services.settings, 'api_token', '')
    entry.app.config['TESTING'] = True
    fake.client = entry.app.test_client()
    fake.tmp = tmp_path
    return fake
