"""
Simulation: the settings, auto jobs (play out by themselves), manual jobs (wait for the
page), fault injection, and the routes the job window uses. No Pi is ever called.
"""
import math
import threading

import pytest

from helpers import apply, finished, motion, motion_finished, motion_job, room_job, simulate
from mcc import services
from mcc.room import Room, RoomConfig, RoomState, Simulation, SimulationError


class Gate:
    """An injected sleep that blocks until the test opens it: a simulated move is then
    "in progress" for exactly as long as the test needs."""

    def __init__(self):
        self.calls = []
        self._open = threading.Event()
        self.waiting = threading.Event()

    def sleep(self, seconds):
        self.calls.append(seconds)
        self.waiting.set()
        self._open.wait(5)

    def open(self):
        self._open.set()


@pytest.fixture
def gate(rig, monkeypatch):
    """services.room with simulated moves held at the gate."""
    gate = Gate()
    tmp = rig.tmp
    monkeypatch.setattr(services, 'room', Room(RoomConfig([str(tmp / 'room.json')]),
                                               RoomState(str(tmp / 'room_state.json')),
                                               Simulation(str(tmp / 'simulation.json')), sleep=gate.sleep))
    yield gate
    gate.open()                       # never leave a job thread blocked


# ════════════════════════════════════════════════════════════════════════════
#  Settings
# ════════════════════════════════════════════════════════════════════════════

def test_defaults():
    assert Simulation(None).settings() == {'on': False, 'mode': 'manual', 'speed': 1.0}


@pytest.mark.parametrize('speed', [1, 1.0, 50, 99.99, 100])
def test_speeds_inside_the_range_are_accepted(speed):
    sim = Simulation(None)
    sim.update(speed=speed)
    assert sim.speed == float(speed)


@pytest.mark.parametrize('speed', [0, 0.99, -1, 100.01, 1000, math.inf, math.nan, True, '5', [5]])
def test_speeds_outside_the_range_or_not_numbers_are_refused(speed):
    with pytest.raises(SimulationError, match='speed'):
        Simulation(None).update(speed=speed)


@pytest.mark.parametrize('settings, field', [
    ({'on': 1}, 'on'), ({'on': 'yes'}, 'on'), ({'mode': 'fast'}, 'mode'), ({'mode': 'AUTO'}, 'mode'),
])
def test_bad_on_or_mode_is_refused(settings, field):
    with pytest.raises(SimulationError, match=field):
        Simulation(None).update(**settings)


def test_update_changes_only_what_is_given():
    sim = Simulation(None)
    sim.update(on=True, mode='auto', speed=20)
    sim.update(speed=5)
    assert sim.settings() == {'on': True, 'mode': 'auto', 'speed': 5.0}


def test_a_refused_update_changes_nothing():
    sim = Simulation(None)
    with pytest.raises(SimulationError):
        sim.update(on=True, speed=0)
    assert sim.settings()['on'] is False


def test_settings_and_positions_are_remembered(tmp_path):
    path = str(tmp_path / 'simulation.json')
    sim = Simulation(path)
    sim.update(on=True, mode='auto', speed=7)
    sim.arrive({'a.lift': 1.2, 'b.z': 0.4})
    sim.forget('b.z')
    again = Simulation(path)
    assert again.settings() == {'on': True, 'mode': 'auto', 'speed': 7.0}
    assert again.position('a.lift') == 1.2 and again.position('b.z') is None


@pytest.mark.parametrize('content', ['{ broken', '[]', '{"on": true, "speed": 0}', '{"axes": "x"}'])
def test_an_unreadable_file_falls_back_to_off(tmp_path, content):
    path = tmp_path / 'simulation.json'
    path.write_text(content, encoding='utf-8')
    assert Simulation(str(path)).on is False


def test_a_fault_is_used_once():
    sim = Simulation(None)
    assert sim.take_fault() is None
    sim.inject_fault(2, 'Jammed')
    assert sim.take_fault() == {'step': 2, 'error': 'Jammed'}
    assert sim.take_fault() is None


@pytest.mark.parametrize('step', [-1, 1.5, True, '1', None])
def test_a_fault_needs_a_step_number(step):
    with pytest.raises(SimulationError):
        Simulation(None).inject_fault(step, 'x')


# ════════════════════════════════════════════════════════════════════════════
#  Auto jobs
# ════════════════════════════════════════════════════════════════════════════

def test_auto_job_plays_out_by_itself_and_sends_nothing(rig):
    simulate(rig, mode='auto', speed=100)
    job = finished(rig, apply(rig, {'a.lift': 1.0, 'b.z': 1.5, 'c.x': 0.5}).get_json()['job']['id'])
    assert job['state'] == 'done' and job['mode'] == 'auto' and job['simulated']
    assert [s['state'] for s in job['steps']] == ['done', 'done', 'done']
    assert rig.sent == []
    sim = services.room.sim
    assert (sim.position('a.lift'), sim.position('b.z'), sim.position('c.x')) == (1.0, 1.5, 0.5)


def test_auto_job_homes_an_unknown_axis_first_only_once(rig):
    simulate(rig, mode='auto', speed=100)
    first = finished(rig, apply(rig, {'a.lift': 1.0}).get_json()['job']['id'])
    second = finished(rig, apply(rig, {'a.lift': 0.7}).get_json()['job']['id'])
    assert first['steps'][0]['homed_first'] and not second['steps'][0]['homed_first']
    assert [e['command'] for e in first['steps'][0]['log']] == ['homing (simulated)', 'moving (simulated)']


def test_tilt_has_nothing_to_home(rig):
    simulate(rig, mode='auto', speed=100)
    job = finished(rig, apply(rig, {'a.tilt': 20}).get_json()['job']['id'])
    assert not job['steps'][0]['homed_first']


def test_auto_job_time_is_scaled_by_the_speed(rig, gate):
    simulate(rig, mode='auto', speed=5)
    services.room.sim.arrive({'a.lift': 0.5})                    # known: no homing
    job = apply(rig, {'a.lift': 1.0}).get_json()['job']
    gate.open()
    finished(rig, job['id'])
    # 0.5 m at 10 m/s = 0.05 s on the rig; 5× faster = 0.01 s
    assert gate.calls == [pytest.approx(0.01)] and job['speed'] == 5.0


def test_auto_job_progress_is_visible_while_moving(rig, gate):
    simulate(rig, mode='auto', speed=1)
    services.room.sim.arrive({'a.lift': 0.5, 'b.z': 0.0})
    job_id = apply(rig, {'a.lift': 1.0, 'b.z': 1.0}).get_json()['job']['id']
    assert gate.waiting.wait(5)
    job = room_job(rig, job_id)
    assert job['state'] == 'running' and job['index'] == 0
    assert [s['state'] for s in job['steps']] == ['moving', 'pending']
    live = rig.client.get('/api/room/live').get_json()
    assert live['axes']['a.lift']['estimated'] is True
    assert services.room.job_holding('pi-01') and services.room.job_holding('pi-02')
    assert rig.client.post('/api/pi/pi-01/command', json={'command': 'SXH'}).status_code == 409
    gate.open()
    assert finished(rig, job_id)['state'] == 'done'


def test_stopping_an_auto_job_lets_the_moving_axis_finish(rig, gate):
    simulate(rig, mode='auto', speed=1)
    job_id = apply(rig, {'a.lift': 1.0, 'a.x': 0.5, 'b.z': 1.0}).get_json()['job']['id']
    assert gate.waiting.wait(5)
    assert rig.client.post('/api/room/stop').get_json()['job']['state'] == 'aborting'
    gate.open()
    job = finished(rig, job_id)
    assert job['state'] == 'aborted'
    assert [s['state'] for s in job['steps']] == ['done', 'skipped', 'skipped']
    assert services.room.sim.position('a.lift') == 1.0 and services.room.sim.position('a.x') is None


@pytest.mark.parametrize('fault_step, states', [
    (0, ['failed', 'skipped', 'skipped']),
    (1, ['done', 'failed', 'skipped']),
    (2, ['done', 'done', 'failed']),
])
def test_an_injected_fault_fails_that_step(rig, fault_step, states):
    simulate(rig, mode='auto', speed=100)
    services.room.sim.inject_fault(fault_step, 'Exceeded Max travel.')
    job = finished(rig, apply(rig, {'a.lift': 1.0, 'a.x': 0.5, 'b.z': 1.0}).get_json()['job']['id'])
    assert job['state'] == 'error' and [s['state'] for s in job['steps']] == states
    failed = job['steps'][fault_step]
    assert failed['command'] in job['error'] and failed['device'] in job['error']
    assert 'Exceeded Max travel.' in job['error']
    assert services.room.sim.position(failed['axis']) is None          # stopped somewhere unknown


def test_a_fault_past_the_last_step_never_fires_and_is_used_up(rig):
    simulate(rig, mode='auto', speed=100)
    services.room.sim.inject_fault(5, 'never')
    assert finished(rig, apply(rig, {'a.lift': 1.0}).get_json()['job']['id'])['state'] == 'done'
    assert finished(rig, apply(rig, {'a.lift': 0.8}).get_json()['job']['id'])['state'] == 'done'


def test_a_fault_waits_for_the_next_auto_job(rig):
    simulate(rig, mode='manual')
    services.room.sim.inject_fault(0, 'Jammed')
    job_id = apply(rig, {'a.lift': 1.0}).get_json()['job']['id']
    rig.client.post(f'/api/room/jobs/{job_id}/finish', json={'result': 'done'})
    simulate(rig, mode='auto', speed=100)
    assert finished(rig, apply(rig, {'a.lift': 0.7}).get_json()['job']['id'])['state'] == 'error'


def test_acqua_sees_an_auto_job_like_a_real_one(rig):
    simulate(rig, mode='auto', speed=100)
    services.monitor.set('pi-02', 'offline')                     # no Pi needed
    job = motion_finished(rig, motion(rig, {'pi-01': {'SYM': 450}, 'pi-02': {'SZM': 1500}}).get_json()['job_id'])
    assert job['state'] == 'done' and job['simulated']
    assert job['positions'] == {'pi-01': {'SYM': 450}, 'pi-02': {'SZM': 1500}}


# ════════════════════════════════════════════════════════════════════════════
#  Manual jobs
# ════════════════════════════════════════════════════════════════════════════

def test_manual_job_waits_for_the_page_then_reports_done(rig):
    simulate(rig, mode='manual')
    job_id = motion(rig, {'pi-01': {'SYM': 450}, 'pi-02': {'SZM': 1500}}).get_json()['job_id']
    assert rig.client.get(f'/api/motion/jobs/{job_id}').get_json()['job']['state'] == 'running'
    assert rig.client.get('/api/room/simulation').get_json()['job']['id'] == job_id
    assert rig.client.post(f'/api/room/jobs/{job_id}/finish', json={'result': 'done'}).status_code == 200
    job = motion_job(rig, job_id)
    assert job['state'] == 'done' and job['positions'] == {'pi-01': {'SYM': 450}, 'pi-02': {'SZM': 1500}}
    assert rig.sent == [] and rig.client.get('/api/room/simulation').get_json()['job'] is None


def test_manual_job_can_be_marked_failed_with_a_reason(rig):
    simulate(rig, mode='manual')
    job_id = motion(rig, {'pi-01': {'SYM': 450}}).get_json()['job_id']
    rig.client.post(f'/api/room/jobs/{job_id}/finish', json={'result': 'error', 'error': 'Jammed'})
    job = motion_job(rig, job_id)
    assert job['state'] == 'error' and 'Jammed' in job['error'] and 'simulated' in job['error']


def test_failed_without_a_reason_still_says_why(rig):
    simulate(rig, mode='manual')
    job_id = motion(rig, {'pi-01': {'SYM': 450}}).get_json()['job_id']
    rig.client.post(f'/api/room/jobs/{job_id}/finish', json={'result': 'error'})
    assert 'Simulated failure' in motion_job(rig, job_id)['error']


def test_stopping_a_manual_job_is_immediate(rig):
    simulate(rig, mode='manual')
    job_id = motion(rig, {'pi-01': {'SYM': 450}, 'pi-02': {'SZM': 10}}).get_json()['job_id']
    assert rig.client.post('/api/room/stop').get_json()['job']['state'] == 'aborted'
    job = motion_job(rig, job_id)
    assert job['state'] == 'aborted' and job['error']
    assert [s['state'] for s in room_job(rig, job_id)['steps']] == ['skipped', 'skipped']


@pytest.mark.parametrize('body', [{}, {'result': 'maybe'}, {'result': True}])
def test_finish_needs_done_or_error(rig, body):
    simulate(rig, mode='manual')
    job_id = motion(rig, {'pi-01': {'SYM': 450}}).get_json()['job_id']
    assert rig.client.post(f'/api/room/jobs/{job_id}/finish', json=body).status_code == 400


def test_finish_refuses_unknown_finished_and_non_manual_jobs(rig):
    assert rig.client.post('/api/room/jobs/nope/finish', json={'result': 'done'}).status_code == 404
    simulate(rig, mode='manual')
    job_id = motion(rig, {'pi-01': {'SYM': 450}}).get_json()['job_id']
    rig.client.post(f'/api/room/jobs/{job_id}/finish', json={'result': 'done'})
    assert rig.client.post(f'/api/room/jobs/{job_id}/finish', json={'result': 'done'}).status_code == 409
    simulate(rig, mode='auto', speed=100)
    auto_id = apply(rig, {'a.lift': 1.0}).get_json()['job']['id']
    finished(rig, auto_id)
    assert rig.client.post(f'/api/room/jobs/{auto_id}/finish', json={'result': 'done'}).status_code == 409


# ════════════════════════════════════════════════════════════════════════════
#  Switching, and what the job window reads
# ════════════════════════════════════════════════════════════════════════════

def test_switching_on_or_off_is_refused_while_a_job_runs_but_mode_and_speed_are_not(rig):
    simulate(rig, mode='manual')
    motion(rig, {'pi-01': {'SYM': 450}})
    assert rig.client.post('/api/room/simulation', json={'on': False}).status_code == 409
    resp = rig.client.post('/api/room/simulation', json={'speed': 20, 'mode': 'auto'})
    assert resp.status_code == 200 and resp.get_json()['speed'] == 20 and resp.get_json()['on'] is True


@pytest.mark.parametrize('body, message', [
    ({'on': 'yes'}, '"on"'), ({'mode': 'fast'}, '"mode"'), ({'speed': 0}, '"speed"'),
    ({'speed': 101}, '"speed"'), ({'speed': 'x'}, '"speed"'),
])
def test_bad_settings_are_refused(rig, body, message):
    resp = rig.client.post('/api/room/simulation', json=body)
    assert resp.status_code == 400 and message in resp.get_json()['error']


def test_settings_route_answers_with_every_setting(rig):
    assert rig.client.post('/api/room/simulation', data='x', content_type='application/json').status_code == 400
    unchanged = rig.client.post('/api/room/simulation', json={}).get_json()
    assert {k: unchanged[k] for k in ('on', 'mode', 'speed')} == {'on': False, 'mode': 'manual', 'speed': 1.0}


def test_current_job_route(rig):
    empty = rig.client.get('/api/room/current').get_json()
    assert empty['job'] is None and empty['simulation']['on'] is False
    simulate(rig, mode='auto', speed=100)
    job_id = apply(rig, {'a.lift': 1.0}).get_json()['job']['id']
    finished(rig, job_id)
    current = rig.client.get('/api/room/current').get_json()
    assert current['job']['id'] == job_id and current['job']['state'] == 'done'
    assert current['simulation'] == {'on': True, 'mode': 'auto', 'speed': 100.0}


def test_pretend_positions_never_leak_into_the_real_state(rig):
    simulate(rig, mode='auto', speed=100)
    finished(rig, apply(rig, {'b.z': 1.5}).get_json()['job']['id'])
    live = rig.client.get('/api/room/live').get_json()
    assert live['simulation'] and live['axes']['b.z']['value'] == 1.5 and live['axes']['b.z']['known']
    rig.client.post('/api/room/simulation', json={'on': False})
    assert rig.client.get('/api/room/live').get_json()['axes']['b.z']['known'] is False
    assert RoomState(str(rig.tmp / 'room_state.json')).position('pi-02', 'SZ') is None


def test_job_window_poll_is_light(rig, monkeypatch):
    """/api/room/current must not ask any Pi anything (every page polls it every second)."""
    asked = []
    monkeypatch.setattr(services.monitor, 'info', asked.append)
    rig.client.get('/api/room/current')
    assert asked == []
