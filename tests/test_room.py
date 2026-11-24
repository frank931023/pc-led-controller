"""
Tests for the room: axis table, setup jobs on the real rig, homing after an Arduino
restart, saved setups and overlays. No Pi needed: pi_client.send_command is replaced by
a scripted fake (conftest.py). The motion API, simulation and scripts have their own files.
"""
import json
import os
import time

import pytest

from helpers import apply, finished, motion, simulate
from mcc import pi_client, services
from mcc.room import Axis, RoomConfig, RoomConfigError, RoomState, firmware_failure, parse_axes
from conftest import AXES
from mcc.setups import SetupStore, clean_overlays


# ----- axis table -----

def test_axis_conversion():
    lift = Axis('a', 'pi-01', 'SY', 0.75, 1.5, 'm', 'Lift', 0.07, 1.0)
    assert lift.move_command(1.2) == 'SYM,450'          # mm from the home end
    assert lift.to_room(450) == pytest.approx(1.2)
    rot = Axis('r', 'pi-01', 'R', -180, 180, 'deg', 'Rot', 12, 0)
    assert rot.move_command(-90) == 'RM,-90'
    assert not Axis('t', 'pi-01', 'T', -15, 35, 'deg', 'Tilt', 6, 0).needs_home


@pytest.mark.parametrize('row, message', [
    ({'id': 'x', 'device': 'pi-01', 'ch': 'Q', 'min': 0, 'max': 1, 'unit': 'm'}, 'ch must be'),
    ({'id': 'x', 'device': 'pi-01', 'ch': 'SX', 'min': 1, 'max': 0, 'unit': 'm'}, 'min must be below max'),
    ({'id': 'x', 'device': 'pi 01', 'ch': 'SX', 'min': 0, 'max': 1, 'unit': 'm'}, 'bad device id'),
    ({'id': 'x', 'device': 'pi-01', 'ch': 'SX', 'min': 'a', 'max': 1, 'unit': 'm'}, 'must be numbers'),
    ({'id': 'x', 'device': None, 'ch': 'SX', 'min': 0, 'max': 1, 'unit': 'm'}, 'device is null'),
    ({'id': 'x', 'device': None, 'ch': None, 'min': 0, 'max': 1, 'unit': 'm', 'verified': True}, 'verified needs'),
    ({'id': 'x', 'device': 'pi-01', 'ch': 'SX', 'min': 0, 'max': 1, 'unit': 'm', 'verified': 'yes'}, 'true or false'),
])
def test_bad_axis_table_is_rejected(row, message):
    with pytest.raises(RoomConfigError, match=message):
        parse_axes({'axes': [row]})


def test_same_firmware_axis_twice_is_rejected():
    row = {'device': 'pi-01', 'ch': 'SX', 'min': 0, 'max': 1, 'unit': 'm'}
    with pytest.raises(RoomConfigError, match='already used'):
        parse_axes({'axes': [{**row, 'id': 'a'}, {**row, 'id': 'b'}]})


def test_bundled_axis_table_is_valid():
    axes = parse_axes(json.load(open(services.BUNDLED_ROOM, encoding='utf-8')))
    assert len(axes) == 19
    assert {a.device for a in axes} == {f'pi-0{i}' for i in range(1, 8)}


def test_broken_edit_keeps_the_last_good_table(tmp_path):
    path = tmp_path / 'room.json'
    path.write_text(json.dumps(AXES), encoding='utf-8')
    config = RoomConfig([str(path)])
    assert len(config.axes()) == 6
    path.write_text('{ broken', encoding='utf-8')
    os.utime(path, (time.time() + 5, time.time() + 5))
    assert len(config.axes()) == 6 and config.info()['error']


# ----- running a setup -----

def test_setup_homes_then_moves_in_table_order(rig):
    resp = apply(rig, {'b.z': 1.0, 'a.x': 0.0, 'a.lift': 1.0, 'a.tilt': 10})
    assert resp.status_code == 202
    job = finished(rig, resp.get_json()['job']['id'])
    assert job['state'] == 'done'
    # table order: a.lift, a.x, a.tilt, b.z; rails are homed first, the tilt servo is not
    assert rig.commands() == ['SYH', 'SYM,500', 'SXH', 'SXM,1000', 'TM,10', 'SZH', 'SZM,1000']
    assert [s['homed_first'] for s in job['steps']] == [True, True, False, True]


def test_homed_axes_are_not_homed_again_and_unchanged_axes_are_skipped(rig):
    finished(rig, apply(rig, {'a.lift': 1.0, 'a.x': 0.0}).get_json()['job']['id'])
    rig.sent.clear()
    job = finished(rig, apply(rig, {'a.lift': 1.0, 'a.x': 0.5}).get_json()['job']['id'])
    assert rig.commands() == ['SXM,1500']
    assert job['unchanged'] == ['a.lift']


def test_every_axis_already_there_finishes_at_once(rig):
    finished(rig, apply(rig, {'a.lift': 1.0}).get_json()['job']['id'])
    job = apply(rig, {'a.lift': 1.0}).get_json()['job']
    assert job['state'] == 'done' and job['steps'] == []


def test_arduino_restart_means_homing_again(rig):
    finished(rig, apply(rig, {'a.lift': 1.0}).get_json()['job']['id'])
    rig.session['pi-01'] = 's2'               # e.g. the Pi rebooted: the status poll shows a new session
    rig.sent.clear()
    finished(rig, apply(rig, {'a.lift': 1.2}).get_json()['job']['id'])
    assert rig.commands() == ['SYH', 'SYM,700']


def test_restart_during_a_move_homes_and_moves_again(rig):
    finished(rig, apply(rig, {'a.lift': 1.0}).get_json()['job']['id'])
    rig.sent.clear()
    rig.restart_on = 'SYM,700'
    job = finished(rig, apply(rig, {'a.lift': 1.2}).get_json()['job']['id'])
    assert job['state'] == 'done'
    assert rig.commands() == ['SYM,700', 'SYH', 'SYM,700']


def test_manual_home_counts(rig):
    resp = rig.client.post('/api/pi/pi-01/command', json={'command': 'SYH'})
    assert resp.status_code == 200
    rig.sent.clear()
    finished(rig, apply(rig, {'a.lift': 1.0}).get_json()['job']['id'])
    assert rig.commands() == ['SYM,500']


def test_exceeded_travel_fails_the_job_and_skips_the_rest(rig):
    rig.extra['SYM,500'] = ['Max Travel = -1', 'Exceeded Max travel.']
    job = finished(rig, apply(rig, {'a.lift': 1.0, 'b.z': 1.0}).get_json()['job']['id'])
    assert job['state'] == 'error'
    assert 'calibrated travel' in job['error']
    assert [s['state'] for s in job['steps']] == ['failed', 'skipped']
    assert ('pi-02', 'SZH') not in rig.sent


def test_firmware_failure_detection():
    assert firmware_failure('SXM,5', {'arduino_result': 'ERR'})
    assert firmware_failure('SXH', {'arduino_result': 'DONE',
                                    'arduino_response': ['ERROR: limit switch timeout on pin 5', 'DONE']})
    assert firmware_failure('SXM,5', {'arduino_result': 'DONE', 'arduino_response': ['DONE']}) is None


# ----- refusing before anything moves -----

@pytest.mark.parametrize('axes, status, problem', [
    ({'nope': 1}, 400, 'Unknown axis'),
    ({'a.lift': 9}, 400, 'outside'),
    ({'a.lift': 'high'}, 400, 'must be a number'),
    ({}, 400, None),
])
def test_invalid_setups_are_refused(rig, axes, status, problem):
    resp = apply(rig, axes)
    assert resp.status_code == status
    if problem:
        assert any(problem in p for p in resp.get_json()['problems'])
    assert rig.sent == []


def test_offline_or_missing_devices_refuse_the_whole_setup(rig):
    services.monitor.set('pi-02', pi_client.OFFLINE)
    resp = apply(rig, {'a.lift': 1.0, 'b.z': 1.0})
    assert resp.status_code == 503                  # not worth retrying (busy would be 409)
    assert resp.get_json()['problems'] == ['pi-02 is offline']
    services.store.remove('pi-02')
    assert 'pi-02 is not in the device list' in apply(rig, {'b.z': 1.0}).get_json()['problems']
    assert rig.sent == []


def test_one_setup_at_a_time_and_manual_commands_wait(rig, monkeypatch):
    import threading
    release = threading.Event()
    send = rig.send

    def slow(device, command, timeout):
        release.wait(5)
        return send(device, command, timeout)

    monkeypatch.setattr(pi_client, 'send_command', slow)
    job = apply(rig, {'a.lift': 1.0}).get_json()['job']
    assert apply(rig, {'b.z': 1.0}).status_code == 409
    manual = rig.client.post('/api/pi/pi-01/command', json={'command': 'SXH'})
    assert manual.status_code == 409 and 'setup is moving' in manual.get_json()['error']
    assert services.room.job_holding('pi-02') is None       # devices outside the setup stay free
    release.set()
    assert finished(rig, job['id'])['state'] == 'done'


def test_stop_skips_the_axes_not_started(rig, monkeypatch):
    import threading
    release = threading.Event()
    send = rig.send

    def slow(device, command, timeout):
        release.wait(5)
        return send(device, command, timeout)

    monkeypatch.setattr(pi_client, 'send_command', slow)
    job = apply(rig, {'a.lift': 1.0, 'b.z': 1.0}).get_json()['job']
    assert rig.client.post('/api/room/stop').status_code == 200
    release.set()
    job = finished(rig, job['id'])
    assert job['state'] == 'aborted'
    assert [s['state'] for s in job['steps']] == ['done', 'skipped']


# ----- live view and saved setups -----

def test_live_view_reports_positions_and_devices(rig):
    finished(rig, apply(rig, {'a.lift': 1.2}).get_json()['job']['id'])
    live = rig.client.get('/api/room/live').get_json()
    assert live['axes']['a.lift'] == {'value': 1.2, 'estimated': False, 'known': True, 'homed': True, 'ready': True}
    assert live['axes']['a.x']['known'] is False and live['axes']['a.x']['estimated'] is True
    assert live['devices']['pi-01']['homed'] == ['SY']
    assert live['job']['state'] == 'done'


def test_state_survives_a_restart_of_the_control_center(rig):
    finished(rig, apply(rig, {'a.lift': 1.2}).get_json()['job']['id'])
    state = RoomState(str(rig.tmp / 'room_state.json'))
    assert state.homed('pi-01', 'SY') and state.position('pi-01', 'SY') == 700


def test_saved_setup_round_trip_and_apply_by_id(rig):
    resp = rig.client.post('/api/room/setups', json={'name': 'Near field', 'axes': {'a.lift': 1.0, 'zzz': 3}})
    assert resp.status_code == 201
    setup = resp.get_json()['setup']
    assert setup['axes'] == {'a.lift': 1.0} and resp.get_json()['dropped'] == ['zzz']
    job = rig.client.post('/api/room/apply', json={'setup_id': setup['id']}).get_json()['job']
    assert job['name'] == 'Near field'
    assert finished(rig, job['id'])['state'] == 'done'
    assert rig.client.delete(f'/api/room/setups/{setup["id"]}').status_code == 200
    assert rig.client.get(f'/api/room/setups/{setup["id"]}').status_code == 404


# ----- examples and overlays -----

def test_examples_are_listed_applicable_and_read_only(rig):
    listed = rig.client.get('/api/room/setups').get_json()['setups']
    example = next(s for s in listed if s['id'] == 'ex-01')
    assert example['readonly'] and example['source'] == 'library'
    job = rig.client.post('/api/room/apply', json={'setup_id': 'ex-01'}).get_json()['job']
    assert finished(rig, job['id'])['state'] == 'done'
    assert rig.client.post('/api/room/setups/ex-01', json={'name': 'x', 'axes': {'a.lift': 1}}).status_code == 403
    assert rig.client.delete('/api/room/setups/ex-01').status_code == 403


def test_overlays_are_saved_cleaned():
    raw = {'points': [{'id': 'dut', 'on': 'tableFront', 'offset': [0, 0.04, 99], 'style': 'dut', 'label': 'DUT'},
                      {'id': 'bad id!', 'on': 'x'}],
           'circles': [{'id': 'c', 'at': 'dut', 'radii': [1, 1.5, -2, 'x'], 'span': 'half', 'level': 'mouth'},
                       {'id': 'empty', 'at': 'dut', 'radii': []}],
           'distances': [{'from': 'hats', 'to': 'dut'}, {'from': 'hats', 'to': 'hats'}]}
    out = clean_overlays(raw)
    assert out['points'] == [{'id': 'dut', 'label': 'DUT', 'on': 'tableFront', 'offset': [0, 0.04, 10], 'style': 'dut'}]
    assert out['circles'][0]['radii'] == [1, 1.5] and out['circles'][0]['span'] == 'half'
    assert len(out['circles']) == 1
    assert out['distances'] == [{'from': 'hats', 'to': 'dut'}]
    assert clean_overlays('nonsense') == {'points': [], 'circles': [], 'distances': []}


def test_saved_setup_keeps_its_overlays(rig):
    overlays = {'circles': [{'id': 'c', 'at': 'hats', 'radii': [0.6]}]}
    setup = rig.client.post('/api/room/setups', json={'name': 'With circle', 'axes': {'a.lift': 1.0},
                                                      'overlays': overlays}).get_json()['setup']
    again = rig.client.get(f'/api/room/setups/{setup["id"]}').get_json()['setup']
    assert again['overlays']['circles'][0]['radii'] == [0.6]


def test_bundled_teams_examples_are_valid():
    store = SetupStore('/nonexistent', services.SETUP_LIBRARY)
    examples = [s for s in store.list() if s.get('group') == 'MS Teams V5']
    assert len(examples) == 14
    axes = parse_axes(json.load(open(services.BUNDLED_ROOM, encoding='utf-8')))
    by_id = {a.id: a for a in axes}
    for s in examples:
        assert set(s['axes']) == set(by_id), s['id']                 # every axis written
        assert all(by_id[k].min <= v <= by_id[k].max for k, v in s['axes'].items()), s['id']
        assert clean_overlays(s['overlays']) == s['overlays'], s['id']        # stored already clean


# ----- wiring not filled in / not verified yet -----

def test_unverified_or_unwired_axes_never_move_for_real(rig):
    resp = apply(rig, {'a.lift': 1.0, 'b.lift': 0.5, 'c.x': 0.5})
    assert resp.status_code == 503
    problems = resp.get_json()['problems']
    assert len(problems) == 2
    assert any('b.lift' in p and '"verified": false' in p for p in problems)
    assert any('c.x' in p and '"device": null' in p for p in problems)
    assert rig.sent == []
    live = rig.client.get('/api/room/live').get_json()
    assert live['axes']['a.lift']['ready'] and not live['axes']['b.lift']['ready']
    axes = {a['id']: a for a in rig.client.get('/api/room/axes').get_json()['axes']}
    assert axes['c.x']['wired'] is False and axes['b.lift']['verified'] is False


def test_unverified_and_unwired_axes_work_in_simulation(rig):
    simulate(rig, mode='manual')
    job = apply(rig, {'b.lift': 0.5, 'c.x': 0.5}).get_json()['job']
    assert [s['command'] for s in job['steps']] == ['SYM,500', '(not wired: 500)']
    assert job['devices'] == ['pi-02']
    # ACQUA can reach a planned (unverified) axis by Pi + command, not an unwired one
    assert motion(rig, {'pi-02': {'SYM': 100}}).status_code == 409        # a job is waiting
    rig.client.post(f'/api/room/jobs/{job["id"]}/finish', json={'result': 'done'})
    assert motion(rig, {'pi-02': {'SYM': 100}}).status_code == 202
