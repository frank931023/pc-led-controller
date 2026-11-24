"""
The 3D room and setups.

  GET  /room                       the 3D room page
  GET  /api/room/axes              the axis table (order = move order)
  GET  /api/room/live              every axis's position, the current job, the devices
  POST /api/room/apply             move to a setup: {"name", "axes": {id: target}} or {"setup_id"}
  POST /api/room/move              move one axis: {"axis", "target"}
  GET  /api/room/jobs[/<id>]       setup jobs (poll one until state is done / error / aborted)
  POST /api/room/stop              stop the running job after the axis moving now
  GET  /api/room/current           the newest job (running or finished) + simulation settings
  GET  /api/room/simulation        simulation settings, and the manual job waiting to be marked done
  POST /api/room/simulation        any of {"on": bool, "mode": "auto" | "manual", "speed": 1 … 100}
  POST /api/room/jobs/<id>/finish  simulation only: {"result": "done"} or {"result": "error", "error"}
  /api/room/setups[/<id>]          saved setups: list / create, read / update / delete

apply and move answer 202 right away with the job; the job runs in the background.
"""

from flask import Blueprint, render_template, request

from .. import services
from ..room import RoomError
from ..setups import SetupError
from ..web import BAD_JSON, fail, json_object, ok

bp = Blueprint('room', __name__)


def _start(targets, name, source):
    try:
        job = services.room.submit(targets, name=name, source=source)
    except RoomError as e:
        return fail(str(e), e.http_status, problems=e.problems)
    return ok(job=job), 202


@bp.get('/room')
def room_page():
    return render_template('room.html')


@bp.get('/api/room/axes')
def room_axes():
    return ok(axes=[a.view() for a in services.room.axes()], config=services.room.config.info())


@bp.get('/api/room/live')
def room_live():
    return ok(**services.room.snapshot())


@bp.post('/api/room/apply')
def room_apply():
    """
    Move the rig to a setup, one axis at a time in axis-table order.
    Either {"name": "...", "axes": {...}} (e.g. from ACQUA) or {"setup_id": "..."} (saved here).
    Optional "source" labels who asked (shown in the job).
    """
    data = json_object()
    if data is None:
        return fail(*BAD_JSON)
    source = str(data.get('source') or 'api')[:32]
    if data.get('setup_id'):
        setup = services.setups.get(str(data['setup_id']))
        if setup is None:
            return fail('Setup not found', 404)
        return _start(setup.get('axes') or {}, setup.get('name'), source)
    return _start(data.get('axes'), str(data.get('name') or '')[:120], source)


@bp.post('/api/room/move')
def room_move():
    data = json_object()
    if data is None:
        return fail(*BAD_JSON)
    axis = str(data.get('axis') or '')
    return _start({axis: data.get('target')}, f'Move {axis}', str(data.get('source') or 'ui')[:32])


@bp.get('/api/room/jobs')
def room_jobs():
    return ok(jobs=services.room.jobs())


@bp.get('/api/room/jobs/<job_id>')
def room_job(job_id):
    job = services.room.job(job_id)
    if job is None:
        return fail('Job not found (finished jobs are kept in memory only, the last 20)', 404)
    return ok(job=job)


@bp.post('/api/room/stop')
@bp.post('/api/room/jobs/<job_id>/stop')
def room_stop(job_id=None):
    """The axis moving now finishes (the firmware has no stop); the rest are skipped."""
    job = services.room.abort(job_id)
    if job is None:
        return fail('No setup is running', 409)
    return ok(job=job)


# ----- simulation (no Pis yet) -----

@bp.route('/api/room/simulation', methods=['GET', 'POST'])
def room_simulation():
    """With simulation on, jobs send nothing to the Pis (see mcc/room/__init__.py)."""
    if request.method == 'POST':
        data = json_object()
        if data is None:
            return fail(*BAD_JSON)
        try:
            services.room.set_simulation(data.get('on'), data.get('mode'), data.get('speed'))
        except RoomError as e:
            return fail(str(e), e.http_status)
    return ok(**services.room.sim.settings(), job=services.room.waiting_job())


@bp.get('/api/room/current')
def room_current():
    """What the job window on every page polls: small, and no Pi is asked anything."""
    return ok(job=services.room.current(), simulation=services.room.sim.settings())


@bp.post('/api/room/jobs/<job_id>/finish')
def room_finish(job_id):
    """Answer a simulated job as if the rig had moved (done) or failed (error)."""
    data = json_object()
    if data is None or data.get('result') not in ('done', 'error'):
        return fail('Send {"result": "done"} or {"result": "error", "error": "why"}', 400)
    try:
        job = services.room.finish_simulated(job_id, data['result'] == 'done',
                                             str(data.get('error') or '')[:300])
    except RoomError as e:
        return fail(str(e), e.http_status)
    return ok(job=job)


# ----- saved setups -----

@bp.route('/api/room/setups', methods=['GET', 'POST'])
def room_setups():
    store = services.setups
    if request.method == 'GET':
        return ok(setups=store.list())
    data = json_object()
    if data is None:
        return fail(*BAD_JSON)
    try:
        setup, dropped = store.save(data, services.room.axes())
    except SetupError as e:
        return fail(str(e), 400)
    return ok(setup=setup, dropped=dropped, setups=store.list()), 201


@bp.route('/api/room/setups/<setup_id>', methods=['GET', 'POST', 'DELETE'])
def room_setup(setup_id):
    store = services.setups
    method = request.method
    if method == 'DELETE':
        try:
            deleted = store.delete(setup_id)
        except SetupError as e:
            return fail(str(e), 403)
        if not deleted:
            return fail('Setup not found', 404)
        return ok(setups=store.list())
    if method == 'GET':
        setup = store.get(setup_id)
        return ok(setup=setup) if setup else fail('Setup not found', 404)
    data = json_object()
    if data is None:
        return fail(*BAD_JSON)
    try:
        setup, dropped = store.save(data, services.room.axes(), setup_id=setup_id)
    except SetupError as e:
        status = 404 if 'not found' in str(e) else 403 if 'read only' in str(e) else 400
        return fail(str(e), status)
    return ok(setup=setup, dropped=dropped, setups=store.list())
