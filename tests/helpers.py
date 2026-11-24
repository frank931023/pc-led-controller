"""Small helpers shared by the test files (the fixtures are in conftest.py)."""

import time

FINISHED = ('done', 'error', 'aborted')


def wait_until(condition, timeout=5, what='condition'):
    """Poll condition() until it returns something truthy; return that value."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(0.005)
    raise AssertionError(f'{what} did not happen within {timeout} s')


def _ended(job):
    return job if job['state'] in FINISHED else None


# ----- the room's own API (/api/room/...) -----

def apply(rig, axes, name='test'):
    return rig.client.post('/api/room/apply', json={'name': name, 'axes': axes})


def room_job(rig, job_id):
    return rig.client.get(f'/api/room/jobs/{job_id}').get_json()['job']


def finished(rig, job_id, timeout=5):
    """The room job once it has ended."""
    return wait_until(lambda: _ended(room_job(rig, job_id)), timeout, f'job {job_id} ending')


# ----- ACQUA's API (/api/motion/...) -----

def motion(rig, targets, label='location2', **extra):
    return rig.client.post('/api/motion/jobs', json={'label': label, 'targets': targets, **extra})


def motion_job(rig, job_id):
    return rig.client.get(f'/api/motion/jobs/{job_id}').get_json()['job']


def motion_finished(rig, job_id, timeout=5):
    return wait_until(lambda: _ended(motion_job(rig, job_id)), timeout, f'motion job {job_id} ending')


# ----- simulation -----

def simulate(rig, **settings):
    """Switch simulation on with the given mode / speed (POST /api/room/simulation)."""
    resp = rig.client.post('/api/room/simulation', json={'on': True, **settings})
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()
