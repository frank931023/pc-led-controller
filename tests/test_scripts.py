"""
Test scripts (Scripts page): every script passes against the fake room, and the runner
behaves: one at a time, Stop, restoring the simulation, failures and crashes reported.
"""
import re
import threading

import pytest

import app as entry
from helpers import wait_until
from mcc import pi_client, services
from mcc.scripts import SCRIPTS, Script, ScriptError, ScriptRunner
from mcc.scripts.runner import KEEP_RUNS, LOG_LIMIT

NEEDS_A_PERSON = {s.id for s in SCRIPTS if s.interactive}


def start(rig, script_id, **body):
    return rig.client.post(f'/api/scripts/{script_id}/run', json=body)


def run_view(rig, run_id):
    return rig.client.get(f'/api/scripts/runs/{run_id}').get_json()['run']


def ended(run):
    return run if run and run['state'] != 'running' else None


def run_ended(rig, run_id, timeout=20):
    return wait_until(lambda: ended(run_view(rig, run_id)), timeout, f'run {run_id} ending')


def run_to_end(rig, script_id, **body):
    resp = start(rig, script_id, **body)
    assert resp.status_code == 202, resp.get_json()
    return run_ended(rig, resp.get_json()['run']['id'])


def log_text(run):
    return '\n'.join(f'{line["level"]}: {line["text"]}' for line in run['log'])


def finish_waiting_job(rig, result='done'):
    """Play the person in front of the job window: mark the waiting job."""
    job = wait_until(lambda: services.room.waiting_job(), what='a job waiting for the page')
    rig.client.post(f'/api/room/jobs/{job["id"]}/finish', json={'result': result, 'error': 'Jammed'})


# ════════════════════════════════════════════════════════════════════════════
#  The catalog
# ════════════════════════════════════════════════════════════════════════════

def test_every_script_is_described_and_has_a_url_safe_unique_id():
    ids = [s.id for s in SCRIPTS]
    assert len(ids) == len(set(ids)) >= 8
    for s in SCRIPTS:
        assert re.fullmatch(r'[a-z0-9-]{3,40}', s.id), s.id
        assert s.name and len(s.description) > 30 and callable(s.run)
        assert s.mode in ('auto', 'manual')
        assert not s.interactive or s.mode == 'manual'          # only a manual job waits for a click


def test_exactly_one_script_needs_a_person():
    assert NEEDS_A_PERSON == {'wait-for-person'}


def test_scripts_are_listed(rig):
    data = rig.client.get('/api/scripts').get_json()
    assert [s['id'] for s in data['scripts']] == [s.id for s in SCRIPTS]
    assert data['run'] is None


# ════════════════════════════════════════════════════════════════════════════
#  Every script passes
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('script_id', [s.id for s in SCRIPTS if s.id not in NEEDS_A_PERSON])
def test_script_passes(rig, script_id):
    run = run_to_end(rig, script_id, speed=100)
    assert run['state'] == 'passed', log_text(run)
    assert run['checks']['failed'] == 0 and run['checks']['passed'] > 0
    assert rig.sent == []                                                   # no Pi was touched
    assert services.room.sim.settings() == {'on': False, 'mode': 'manual', 'speed': 1.0}  # restored
    assert services.room.sim.take_fault() is None
    current = services.room.current()
    assert current is None or current['state'] not in ('running', 'aborting')


def test_wait_for_person_passes_when_marked_done(rig):
    threading.Thread(target=finish_waiting_job, args=(rig,), daemon=True).start()
    run = run_to_end(rig, 'wait-for-person')
    assert run['state'] == 'passed', log_text(run)


def test_wait_for_person_fails_when_marked_failed(rig):
    threading.Thread(target=finish_waiting_job, args=(rig, 'error'), daemon=True).start()
    run = run_to_end(rig, 'wait-for-person')
    assert run['state'] == 'failed' and 'Jammed' in log_text(run)


def test_library_sequence_sends_every_example(rig):
    run = run_to_end(rig, 'library-sequence', speed=100)
    text = log_text(run)
    assert '"Example"' in text and '"Example 2"' in text and 'Setup 2 of 2' in text


def test_scripts_work_with_an_api_token(rig, monkeypatch):
    monkeypatch.setattr(services.settings, 'api_token', 'secret')
    assert start(rig, 'one-setup').status_code == 401
    resp = rig.client.post('/api/scripts/one-setup/run', json={}, headers={'Authorization': 'Bearer secret'})
    run = run_ended(rig, resp.get_json()['run']['id'])
    assert run['state'] == 'passed', log_text(run)


# ════════════════════════════════════════════════════════════════════════════
#  Starting
# ════════════════════════════════════════════════════════════════════════════

def test_unknown_script_and_unknown_run_are_404(rig):
    assert start(rig, 'nope').status_code == 404
    assert rig.client.get('/api/scripts/runs/nope').status_code == 404


@pytest.mark.parametrize('speed', [0, 0.5, 101, 'fast', True, None])
def test_speed_outside_1_to_100_is_refused(rig, speed):
    resp = start(rig, 'one-setup', speed=speed)
    assert resp.status_code == 400 and 'speed' in resp.get_json()['error']


@pytest.mark.parametrize('speed', [1, 100])
def test_speed_at_the_edges_is_accepted(rig, speed):
    assert run_to_end(rig, 'same-twice', speed=speed)['speed'] == speed


def test_a_script_can_cap_the_speed(rig):
    assert run_to_end(rig, 'stop-midway', speed=100)['speed'] == 5


def test_body_must_be_json(rig):
    resp = rig.client.post('/api/scripts/one-setup/run', data='x', content_type='application/json')
    assert resp.status_code == 400


def test_only_one_script_at_a_time(rig):
    first = start(rig, 'wait-for-person').get_json()['run']
    second = start(rig, 'one-setup')
    assert second.status_code == 409 and 'still running' in second.get_json()['error']
    rig.client.post('/api/scripts/stop')
    run_ended(rig, first['id'])


def test_not_while_a_real_setup_is_moving(rig, monkeypatch):
    release = threading.Event()

    def held_back(device, command, timeout):
        release.wait(5)
        return rig.send(device, command, timeout)

    monkeypatch.setattr(pi_client, 'send_command', held_back)
    rig.client.post('/api/room/apply', json={'axes': {'a.lift': 1.0}})
    resp = start(rig, 'one-setup')
    assert resp.status_code == 409 and 'setup is moving' in resp.get_json()['error']
    release.set()


# ════════════════════════════════════════════════════════════════════════════
#  Stopping and restoring
# ════════════════════════════════════════════════════════════════════════════

def test_stop_ends_the_run_and_its_job_and_restores_the_settings(rig):
    services.room.sim.update(on=False, mode='auto', speed=7)
    run_id = start(rig, 'wait-for-person').get_json()['run']['id']
    job = wait_until(lambda: services.room.waiting_job(), what='the job window job')
    assert rig.client.post('/api/scripts/stop').status_code == 200
    run = run_ended(rig, run_id)
    assert run['state'] == 'stopped'
    assert services.room.job(job['id'])['state'] == 'aborted'
    assert services.room.sim.settings() == {'on': False, 'mode': 'auto', 'speed': 7.0}


def test_stop_with_nothing_running_is_409(rig):
    assert rig.client.post('/api/scripts/stop').status_code == 409


# ════════════════════════════════════════════════════════════════════════════
#  The runner with hand-made scripts
# ════════════════════════════════════════════════════════════════════════════

def make_runner(*fns):
    return ScriptRunner([Script(fn.__name__.replace('_', '-'), fn.__name__, 'x' * 40, fn) for fn in fns],
                        sleep=lambda s: None, poll_interval=0.001)


def run_alone(runner, script_id):
    run = runner.start(script_id, entry.app)
    return wait_until(lambda: ended(runner.get(run['id'])))


def test_a_failed_check_fails_the_run_but_the_script_goes_on(rig):
    def two_checks(ctx):
        ctx.check(False, 'first', 'why')
        ctx.check(True, 'second')

    run = run_alone(make_runner(two_checks), 'two-checks')
    assert run['state'] == 'failed' and run['checks'] == {'passed': 1, 'failed': 1}
    assert [line['level'] for line in run['log'][-2:]] == ['fail', 'pass']
    assert run['log'][-2]['text'] == 'first — why'


def test_require_stops_the_script(rig):
    def needs_it(ctx):
        ctx.require(False, 'needed')
        ctx.check(True, 'never reached')

    run = run_alone(make_runner(needs_it), 'needs-it')
    assert run['state'] == 'failed' and run['checks'] == {'passed': 0, 'failed': 1}


def test_a_crashing_script_ends_as_error_and_still_cleans_up(rig):
    def crashes(ctx):
        ctx.room.sim.inject_fault(0, 'left behind')
        raise KeyError('boom')

    run = run_alone(make_runner(crashes), 'crashes')
    assert run['state'] == 'error' and 'KeyError' in log_text(run)
    assert services.room.sim.take_fault() is None and services.room.sim.on is False


def test_the_log_is_capped(rig):
    def chatty(ctx):
        for i in range(LOG_LIMIT + 50):
            ctx.log(f'line {i}')

    run = run_alone(make_runner(chatty), 'chatty')
    assert len(run['log']) == LOG_LIMIT + 1 and 'Log limit' in run['log'][-1]['text']
    assert run['state'] == 'passed'


def test_only_the_last_runs_are_kept(rig):
    def quick(ctx):
        ctx.check(True, 'ok')

    runner = make_runner(quick)
    first = run_alone(runner, 'quick')
    for _ in range(KEEP_RUNS):
        run_alone(runner, 'quick')
    assert runner.get(first['id']) is None and runner.latest()['state'] == 'passed'


def test_runner_errors_carry_the_http_status(rig):
    with pytest.raises(ScriptError) as e:
        make_runner().start('missing', entry.app)
    assert e.value.http_status == 404
