"""
The motion API ACQUA Automation uses (POST /api/motion/jobs, GET /api/motion/jobs/<id>),
on the real-rig path with fake Pis: translation, order, refusals, failures, edge values.
"""
import threading

import pytest

from helpers import motion, motion_finished
from mcc import pi_client, services

# a.lift = pi-01 SY, travel 0.5 … 1.5 m  ->  0 … 1000 mm
# a.tilt = pi-01 T,  -15 … 35°
# b.z    = pi-02 SZ, 0 … 2 m            ->  0 … 2000 mm


# ----- the normal path -----

def test_firmware_targets_run_in_axis_table_order(rig):
    # sent in "wrong" order: the axis table decides (a.lift, a.x, then pi-02)
    resp = motion(rig, {'pi-02': {'SZM': 1500}, 'pi-01': {'SXM': 1250, 'SYM': 450}})
    assert resp.status_code == 202 and resp.get_json()['status'] == 'success'
    job = motion_finished(rig, resp.get_json()['job_id'])
    assert job['state'] == 'done' and job['error'] is None and job['label'] == 'location2'
    assert rig.commands() == ['SYH', 'SYM,450', 'SXH', 'SXM,1250', 'SZH', 'SZM,1500']
    assert job['positions'] == {'pi-01': {'SYM': 450, 'SXM': 1250}, 'pi-02': {'SZM': 1500}}
    assert job['progress'] == {'done': 3, 'total': 3, 'unchanged': 0}
    assert job['simulated'] is False and job['warnings'] == []


def test_same_targets_again_move_nothing_and_are_done_at_once(rig):
    motion_finished(rig, motion(rig, {'pi-01': {'SYM': 450}}).get_json()['job_id'])
    sent = len(rig.sent)
    resp = motion(rig, {'pi-01': {'SYM': 450}})
    assert resp.get_json()['job']['state'] == 'done'
    again = motion_finished(rig, resp.get_json()['job_id'])
    assert again['progress'] == {'done': 0, 'total': 0, 'unchanged': 1} and len(rig.sent) == sent


def test_positions_are_only_reported_once_the_job_ended(rig, monkeypatch):
    release = threading.Event()

    def held_back(device, command, timeout):
        release.wait(5)
        return rig.send(device, command, timeout)

    monkeypatch.setattr(pi_client, 'send_command', held_back)
    job_id = motion(rig, {'pi-01': {'SYM': 450}}).get_json()['job_id']
    running = rig.client.get(f'/api/motion/jobs/{job_id}').get_json()['job']
    assert running['state'] == 'running' and 'positions' not in running
    release.set()
    assert 'positions' in motion_finished(rig, job_id)


def test_label_and_source(rig):
    job_id = motion(rig, {'pi-01': {'SYM': 1}}, label='x' * 500, source='acqua-test').get_json()['job_id']
    motion_finished(rig, job_id)
    job = services.room.job(job_id)
    assert job['name'] == 'x' * 120 and job['source'] == 'acqua-test'
    job_id = motion(rig, {'pi-01': {'SYM': 2}}, label='').get_json()['job_id']
    assert services.room.job(job_id)['name'] == 'ACQUA setup'
    assert services.room.job(job_id)['source'] == 'acqua'


def test_tilt_in_a_job_comes_with_a_warning(rig):
    job = motion_finished(rig, motion(rig, {'pi-01': {'TM': 10}}).get_json()['job_id'])
    assert job['state'] == 'done' and 'TM' in job['warnings'][0]


# ----- edge values of the travel -----

@pytest.mark.parametrize('key, value', [
    ('SYM', 0), ('SYM', 1000),          # both ends of a rail (0 mm = the home switch)
    ('TM', -15), ('TM', 35),            # both ends of the tilt
    ('SYM', 449.6),                     # not an integer: rounded to 450 mm
])
def test_values_on_the_edge_of_the_travel_are_accepted(rig, key, value):
    resp = motion(rig, {'pi-01': {key: value}})
    assert resp.status_code == 202, resp.get_json()
    job = motion_finished(rig, resp.get_json()['job_id'])
    assert job['state'] == 'done' and job['positions']['pi-01'][key] == round(value)


@pytest.mark.parametrize('key, value', [('SYM', -1), ('SYM', 1001), ('TM', -16), ('TM', 36), ('SZM', 1e9)])
def test_values_just_outside_the_travel_are_refused(rig, key, value):
    device = 'pi-02' if key == 'SZM' else 'pi-01'
    resp = motion(rig, {device: {key: value}})
    assert resp.status_code == 400 and 'outside its travel' in resp.get_json()['error']
    assert rig.sent == []


# ----- refusals: nothing moves, one readable "error" -----

@pytest.mark.parametrize('targets, message', [
    ({'pi-09': {'SZM': 1}}, 'pi-09: no such device'),
    ({'pi-001': {'SZM': 1}}, 'pi-001: no such device'),          # ACQUA's old three-digit ids
    ({'pi-01': {'XYZ': 1}}, 'unknown command XYZ'),
    ({'pi-01': {'sym': 1}}, 'unknown command sym'),               # case matters, like the firmware
    ({'pi-01': {'SYH': 1}}, 'unknown command SYH'),               # only absolute moves
    ({'pi-01': {'SZM': 1}}, 'nothing is wired to pi-01 SZ'),
    ({'pi-01': {'SYM': 'high'}}, 'not a number'),
    ({'pi-01': {'SYM': '450'}}, 'not a number'),                  # no numbers as text
    ({'pi-01': {'SYM': True}}, 'not a number'),                   # bool is not a number
    ({'pi-01': {'SYM': None}}, 'not a number'),
    ({'pi-01': {'SYM': [450]}}, 'not a number'),
    ({'pi-01': {}}, 'no commands'),
    ({'pi-01': [450]}, 'no commands'),
    ({}, 'non-empty'),
    ([], 'non-empty'),
    ('pi-01', 'non-empty'),
    (None, 'non-empty'),
])
def test_bad_targets_are_refused_with_one_readable_error(rig, targets, message):
    resp = motion(rig, targets)
    assert resp.status_code == 400 and message in resp.get_json()['error']
    assert rig.sent == [] and services.room.current() is None


def test_every_problem_is_listed_not_just_the_first(rig):
    resp = motion(rig, {'pi-01': {'SYM': 5000, 'XYZ': 1}, 'pi-09': {'SXM': 1}})
    problems = resp.get_json()['problems']
    assert len(problems) == 3 and resp.get_json()['error'] == '; '.join(problems)


def test_one_bad_axis_refuses_the_whole_job(rig):
    resp = motion(rig, {'pi-01': {'SYM': 450}, 'pi-02': {'SZM': 99999}})
    assert resp.status_code == 400 and rig.sent == []


@pytest.mark.parametrize('raw', ['not json', '[1, 2]', '"text"', '', '{"targets": {"pi-01": {"SYM": NaN}}}'])
def test_broken_bodies_are_refused(rig, raw):
    resp = rig.client.post('/api/motion/jobs', data=raw, content_type='application/json')
    assert resp.status_code == 400 and resp.get_json()['status'] == 'error'
    assert rig.sent == []


def test_unknown_job_is_404(rig):
    resp = rig.client.get('/api/motion/jobs/nope')
    assert resp.status_code == 404 and 'No such job' in resp.get_json()['error']


# ----- refusals because of the devices or the rig -----

def test_offline_device_is_503_busy_device_is_409(rig):
    services.monitor.set('pi-02', pi_client.OFFLINE)
    resp = motion(rig, {'pi-02': {'SZM': 100}})
    assert resp.status_code == 503 and 'pi-02 is offline' in resp.get_json()['error']
    services.monitor.set('pi-02', pi_client.ONLINE)
    assert services.monitor.try_begin('pi-02', 'SZC')
    resp = motion(rig, {'pi-02': {'SZM': 100}})
    assert resp.status_code == 409 and 'pi-02 is running SZC' in resp.get_json()['error']
    services.monitor.end('pi-02')


def test_unverified_axis_is_503_on_the_real_rig(rig):
    resp = motion(rig, {'pi-02': {'SYM': 100}})                 # b.lift: planned, not verified
    assert resp.status_code == 503 and 'b.lift' in resp.get_json()['error']


def test_api_token_is_required_when_set(rig, monkeypatch):
    monkeypatch.setattr(services.settings, 'api_token', 'secret')
    assert motion(rig, {'pi-01': {'SYM': 1}}).status_code == 401
    wrong = rig.client.post('/api/motion/jobs', json={'targets': {'pi-01': {'SYM': 1}}},
                            headers={'Authorization': 'Bearer nope'})
    assert wrong.status_code == 401
    right = rig.client.post('/api/motion/jobs', json={'targets': {'pi-01': {'SYM': 1}}},
                            headers={'Authorization': 'Bearer secret'})
    assert right.status_code == 202


# ----- failures while moving -----

def test_exceeded_travel_is_an_error_not_done(rig):
    rig.extra['SYM,450'] = ['Exceeded Max travel.']
    job = motion_finished(rig, motion(rig, {'pi-01': {'SYM': 450}}).get_json()['job_id'])
    assert job['state'] == 'error' and job['error'].startswith('pi-01 ')
    assert 'SYM,450' in job['error'] and 'calibrated travel' in job['error']
    assert 'SYM' not in job['positions'].get('pi-01', {})


def test_err_from_the_firmware_is_an_error(rig, monkeypatch):
    def unknown_move(device, command, timeout):
        body = rig.send(device, command, timeout)
        return {**body, 'arduino_result': 'ERR'} if command.startswith('SYM') else body

    monkeypatch.setattr(pi_client, 'send_command', unknown_move)
    job = motion_finished(rig, motion(rig, {'pi-01': {'SYM': 450}}).get_json()['job_id'])
    assert job['state'] == 'error' and 'does not know SYM,450' in job['error']


def test_pi_going_offline_mid_job_is_an_error(rig, monkeypatch):
    def offline(device, command, timeout):
        raise pi_client.PiError('Cannot reach pi-01', pi_client.OFFLINE)

    monkeypatch.setattr(pi_client, 'send_command', offline)
    job = motion_finished(rig, motion(rig, {'pi-01': {'SYM': 450}}).get_json()['job_id'])
    assert job['state'] == 'error' and 'Cannot reach pi-01' in job['error']
