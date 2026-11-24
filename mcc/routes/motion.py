"""
Motion jobs for ACQUA Automation (its docs/MCC_MOTION_API.md).

  POST /api/motion/jobs          {"label": "location2",
                                  "targets": {"pi-03": {"SZM": 3350, "SXM": 1100, "RM": 0}, ...}}
                                 -> 202 {"job_id"}
  GET  /api/motion/jobs/<id>     -> {"job": {job_id, label, state, error, positions, ...}}

ACQUA sends firmware values (mm from the home switch / degrees) per Pi and command. This is
only a translation layer: every (device, command) is looked up in the axis table
(room.json: pi-03 + SZ = hats.z), turned back into room units and handed to the same setup
job the 3D room uses. So the order (axis-table order, one axis at a time), homing after
an Arduino restart, the checks before anything moves and simulation all apply as they are.

Errors are a single human-readable "error" string: ACQUA records that text as the reason
the setup was not applied.
"""

import math
import re

from flask import Blueprint

from .. import services
from ..room import RoomError
from ..web import BAD_JSON, fail, json_object, ok

bp = Blueprint('motion', __name__)

MOVE_KEY = re.compile(r'^(SX|SY|SZ|R|T)M$')
STATES = {'running': 'running', 'aborting': 'running'}     # others map to themselves
TILT_WARNING = 'TM (head tilt) is not implemented in the firmware yet: the head does not move'


def _travel(ax):
    """The axis's travel in firmware units (what ACQUA sends)."""
    return (0, ax.to_device(ax.max)) if ax.unit == 'm' else (int(math.ceil(ax.min)), int(math.floor(ax.max)))


def to_room_targets(targets):
    """
    {device: {"SZM": value}} -> ({axis id: room value}, problems).
    Everything is checked before a single axis moves.
    """
    if not isinstance(targets, dict) or not targets:
        return None, ['targets must be a non-empty object: {"<device id>": {"SZM": 3350, ...}}']
    wiring = {(ax.device, ax.ch): ax for ax in services.room.axes() if ax.wired}
    devices = {device for device, _ in wiring}
    out, problems = {}, []
    for device, commands in targets.items():
        if device not in devices:
            problems.append(f'{device}: no such device (not in the axis table, room.json)')
            continue
        if not isinstance(commands, dict) or not commands:
            problems.append(f'{device}: no commands')
            continue
        for key, value in commands.items():
            m = MOVE_KEY.match(str(key))
            if not m:
                problems.append(f'{device}: unknown command {key} (use SXM / SYM / SZM / RM / TM)')
                continue
            ax = wiring.get((device, m.group(1)))
            if ax is None:
                problems.append(f'{device} {key}: nothing is wired to {device} {m.group(1)} in room.json')
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                problems.append(f'{device} {key}: not a number ({value!r})')
                continue
            low, high = _travel(ax)
            if not low <= value <= high:
                problems.append(f'{device} {key},{value:g}: outside its travel {low} … {high} '
                                f'{"mm" if ax.unit == "m" else "°"} ({ax.id})')
                continue
            out[ax.id] = ax.to_room(round(value))
    return out, problems


def motion_view(job):
    """A setup job in the shape ACQUA polls for."""
    by_id = {ax.id: ax for ax in services.room.axes()}
    finished = job['state'] not in STATES
    steps = job['steps']
    view = {
        'job_id': job['id'],
        'label': job['name'],
        'state': STATES.get(job['state'], job['state']),
        'error': job['error'] if job['state'] in ('error', 'aborted') else None,
        # extras ACQUA may ignore: progress, time estimate, simulation, warnings
        'progress': {'done': sum(s['state'] == 'done' for s in steps), 'total': len(steps),
                     'unchanged': len(job['unchanged'])},
        'eta_seconds': job['eta'],
        'simulated': job['simulated'],
        'warnings': [TILT_WARNING] if any(by_id.get(a) and by_id[a].ch == 'T' for a in job['targets']) else [],
    }
    if job['state'] == 'aborted' and not view['error']:
        view['error'] = 'Stopped from the control center'
    if finished:
        # where every requested axis really ended up (as far as the MCC knows)
        view['positions'] = services.room.device_positions(set(job['targets']), job['simulated'])
    return view


@bp.post('/api/motion/jobs')
def create_job():
    data = json_object()
    if data is None:
        return fail(*BAD_JSON)
    axes, problems = to_room_targets(data.get('targets'))
    if problems:
        return fail('; '.join(problems), 400, problems=problems)
    label = str(data.get('label') or '')[:120] or 'ACQUA setup'
    try:
        job = services.room.submit(axes, name=label, source=str(data.get('source') or 'acqua')[:32])
    except RoomError as e:
        message = f'{e}: {"; ".join(e.problems)}' if e.problems else str(e)
        return fail(message, e.http_status, problems=e.problems)
    return ok(job_id=job['id'], job=motion_view(job)), 202


@bp.get('/api/motion/jobs/<job_id>')
def get_job(job_id):
    job = services.room.job(job_id)
    if job is None:
        return fail('No such job (jobs are kept in memory: the control center may have restarted)', 404)
    return ok(job=motion_view(job))
