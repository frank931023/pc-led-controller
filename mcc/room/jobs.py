"""
Setup jobs: check a setup completely, then move the room there one axis at a time.

A job's mode is fixed when it is submitted:
  rig     real hardware (RigRunner)
  auto    simulation that plays out by itself (SimulatedRunner)
  manual  simulation that waits for someone to mark it done / failed on the web page
"""

import logging
import math
import threading
import time
import uuid
from collections import OrderedDict

from .. import pi_client, services
from .axes import firmware_failure
from .runners import RigRunner, SimulatedRunner, StepFailed
from .state import Simulation, SimulationError

log = logging.getLogger('mcc.room')

ACTIVE = ('running', 'aborting')
KEEP_JOBS = 20          # finished jobs kept for polling (memory only)


class RoomError(Exception):
    """A request was refused before anything moved. problems lists every reason."""

    def __init__(self, message, http_status, problems=()):
        super().__init__(message)
        self.http_status = http_status
        self.problems = list(problems)


# ════════════════════════════════════════════════════════════════════════════
#  Checks done before a job is accepted (each raises RoomError)
# ════════════════════════════════════════════════════════════════════════════

def check_targets(by_id, targets):
    """{axis id: float} from a setup's targets, or RoomError 400 listing every problem."""
    if not isinstance(targets, dict) or not targets:
        raise RoomError('No axes to move: send {"axes": {"<axis id>": <target>, ...}}', 400)
    problems, wanted = [], {}
    for aid, raw in targets.items():
        ax = by_id.get(aid)
        if ax is None:
            problems.append(f'Unknown axis "{aid}"')
        elif isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
            problems.append(f'{aid}: target must be a number, got {raw!r}')
        elif not ax.min <= raw <= ax.max:
            problems.append(f'{aid} ({ax.label}): {raw:g} is outside {ax.min:g} … {ax.max:g} {ax.unit}')
        else:
            wanted[aid] = float(raw)
    if problems:
        raise RoomError('The setup has invalid axes', 400, problems)
    return wanted


def check_ready_for_rig(by_id, wanted):
    """An axis nobody has checked on the real rig never moves for real (503)."""
    unwired = [aid for aid in wanted if not by_id[aid].wired]
    unverified = [aid for aid in wanted if by_id[aid].wired and not by_id[aid].verified]
    problems = []
    if unverified:
        problems.append('Not verified on the real rig yet (room.json "verified": false): ' + ', '.join(unverified))
    if unwired:
        problems.append('Not wired to a Pi yet (room.json "device": null): ' + ', '.join(unwired))
    if problems:
        raise RoomError('Some axes are not set up for the real rig yet', 503, problems)


def check_devices(devices):
    """Busy (worth retrying later) is 409; offline / missing / no Arduino is 503."""
    busy, down = [], []
    for device_id in devices:
        if services.store.get(device_id) is None:
            down.append(f'{device_id} is not in the device list')
        elif services.monitor.running(device_id):
            busy.append(f'{device_id} is running {services.monitor.running(device_id)}')
        elif services.monitor.status(device_id) != pi_client.ONLINE:
            down.append(f'{device_id} is {services.monitor.status(device_id)}')
    if busy or down:
        raise RoomError('Some devices cannot move right now', 409 if busy else 503, busy + down)


# ════════════════════════════════════════════════════════════════════════════
#  The room
# ════════════════════════════════════════════════════════════════════════════

class Room:
    def __init__(self, config, state, simulation=None, sleep=time.sleep):
        self.config = config
        self.state = state
        self.sim = simulation or Simulation(None)
        self._lock = threading.RLock()
        self._jobs = OrderedDict()          # id -> job (newest last)
        self._current = None                # id of the newest job
        self._runners = {'rig': RigRunner(self), 'auto': SimulatedRunner(self, sleep)}

    # ----- axes and positions -----

    def axes(self):
        return self.config.axes()

    def _axis_table(self):
        by_id = {a.id: a for a in self.config.axes()}
        if not by_id:
            raise RoomError(f'The axis table is not usable: {self.config.info()["error"]}', 503)
        return by_id

    def sync_sessions(self, device_ids):
        for device_id in device_ids:
            self.state.note_session(device_id, services.monitor.info(device_id).get('arduino_session'))

    def position(self, ax, simulated=False):
        """Where the axis is in firmware units, or None when that is not known."""
        if simulated:
            value = self.sim.position(ax.id)
            return None if value is None else ax.to_device(value)
        if ax.needs_home and not self.state.homed(ax.device, ax.ch):
            return None
        return self.state.position(ax.device, ax.ch)

    def device_positions(self, axis_ids, simulated=False):
        """{device: {"<ch>M": firmware value}} for the given axes whose position is known
        (the format ACQUA sends its targets in)."""
        out = {}
        for ax in self.config.axes():
            if ax.id in axis_ids and ax.wired:
                pos = self.position(ax, simulated)
                if pos is not None:
                    out.setdefault(ax.device, {})[ax.move_key] = pos
        return out

    # ----- called for every command (see dispatch.run) -----

    def observe(self, device_id, command, body):
        self.state.observe(device_id, command, body, failed=firmware_failure(command, body) is not None)

    def job_holding(self, device_id):
        """Name of the running setup that owns this device, or None."""
        with self._lock:
            job = self._jobs.get(self._current)
            if job and job['state'] in ACTIVE and device_id in job['devices']:
                return job['name']
            return None

    # ----- submitting -----

    def submit(self, targets, name='', source='api'):
        """
        Check a setup completely and start moving. A setup that cannot be completed is
        refused before a single axis moves. Raises RoomError. Returns the job (state
        "done" right away when every axis is already there).
        """
        by_id = self._axis_table()
        wanted = check_targets(by_id, targets)
        settings = self.sim.settings()
        mode = settings['mode'] if settings['on'] else 'rig'
        if mode == 'rig':
            check_ready_for_rig(by_id, wanted)
        devices = list(OrderedDict.fromkeys(
            by_id[aid].device for aid in by_id if aid in wanted and by_id[aid].wired))

        with self._lock:
            self._check_idle()
            if mode == 'rig':
                check_devices(devices)
                self.sync_sessions(devices)
            job = self._plan(by_id, wanted, devices, name, source, mode, settings['speed'])
            self._jobs[job['id']] = job
            self._current = job['id']
            self._trim()
            view = self._view(job)
        self._start(job)
        return view

    def _check_idle(self):
        current = self._jobs.get(self._current)
        if current and current['state'] in ACTIVE:
            raise RoomError(f'Another setup is still moving: "{current["name"]}"', 409)

    def _plan(self, by_id, wanted, devices, name, source, mode, speed):
        """Steps in axis-table order; axes already at their target are left out."""
        simulated = mode != 'rig'
        speed = speed if mode == 'auto' else 1.0
        steps, unchanged = [], []
        for ax in by_id.values():
            if ax.id not in wanted:
                continue
            target = wanted[ax.id]
            pos = self.position(ax, simulated)
            if pos is not None and pos == ax.to_device(target):
                unchanged.append(ax.id)
                continue
            start = ax.to_room(pos) if pos is not None else ax.default
            steps.append({
                'axis': ax.id, 'label': ax.label, 'device': ax.device, 'unit': ax.unit,
                'from': round(start, 4), 'to': target, 'known_from': pos is not None,
                'command': ax.move_command(target),
                'seconds': round(ax.seconds(start, target), 1),
                'state': 'pending', 'homed_first': False, 'error': None,
                'started': None, 'finished': None, 'log': [],
                '_axis': ax, '_phase': None, '_simulated': simulated,
            })
        now = time.time()
        return {
            'id': uuid.uuid4().hex[:12], 'name': name or 'Setup', 'source': source,
            'mode': mode, 'simulated': simulated, 'speed': speed,
            'state': 'running' if steps else 'done', 'error': None,
            'created': now, 'started': now, 'finished': None if steps else now,
            'index': 0, 'steps': steps, 'unchanged': unchanged, 'targets': wanted,
            'devices': devices, 'eta': round(sum(s['seconds'] for s in steps) / speed, 1),
            '_fault': self.sim.take_fault() if mode == 'auto' else None,
        }

    def _trim(self):
        while len(self._jobs) > KEEP_JOBS:
            oldest = next(iter(self._jobs))
            if self._jobs[oldest]['state'] in ACTIVE:
                break
            del self._jobs[oldest]

    def _start(self, job):
        """Run the job in the background. A manual simulated job has no runner: it waits
        for finish_simulated()."""
        runner = self._runners.get(job['mode'])
        if job['state'] == 'running' and runner:
            threading.Thread(target=self._run, args=(job, runner), daemon=True, name='room-job').start()

    # ----- running (the same loop for every runner) -----

    def _run(self, job, runner):
        for index, step in enumerate(job['steps']):
            with self._lock:
                if job['state'] == 'aborting':
                    self._finish(job, 'aborted', skip_from=index)
                    return
                job['index'] = index
                step['started'] = time.time()
            try:
                runner.run_step(job, step)
            except StepFailed as e:
                self._fail(job, step, index, str(e))
                return
            except Exception as e:                      # a bug must not leave the job "running"
                log.exception('Setup "%s" crashed', job['name'])
                self._fail(job, step, index, f'Internal error: {e}')
                return
            with self._lock:
                step['state'], step['finished'] = 'done', time.time()
        with self._lock:
            job['index'] = len(job['steps'])
            self._finish(job, 'aborted' if job['state'] == 'aborting' else 'done')

    def _fail(self, job, step, index, reason):
        with self._lock:
            step['state'], step['error'], step['finished'], step['_phase'] = 'failed', reason, time.time(), None
            job['error'] = f'{step["device"]} {step["label"]}: {reason}'
            self._finish(job, 'error', skip_from=index + 1)
        log.warning('Setup "%s" failed: %s', job['name'], job['error'])

    def _finish(self, job, state, skip_from=None):
        """Caller holds the lock."""
        if skip_from is not None:
            for s in job['steps'][skip_from:]:
                s['state'] = 'skipped'
        job['state'], job['finished'] = state, time.time()

    def begin_phase(self, step, phase, goal, speed=1.0):
        """A step starts homing / moving towards goal. Returns how long it should take."""
        with self._lock:
            start = self.estimate(step)
            seconds = step['_axis'].seconds(start, goal) / speed
            step['state'] = phase
            step['_phase'] = {'from': start, 'to': goal, 'at': time.time(), 'seconds': seconds}
            return seconds

    def end_phase(self, step, command, response):
        with self._lock:
            step['log'].append({'command': command, 'response': response})
            step['_phase'] = None

    # ----- reading -----

    def estimate(self, step):
        """Where a step's axis probably is now (room units): linear from start to goal over
        the phase's duration. The serial line is busy until DONE, so it cannot be asked."""
        phase = step.get('_phase')
        if not phase:
            pos = self.position(step['_axis'], step['_simulated'])
            return step['_axis'].to_room(pos) if pos is not None else step['from']
        frac = 1.0 if phase['seconds'] <= 0 else min(1.0, (time.time() - phase['at']) / phase['seconds'])
        return phase['from'] + (phase['to'] - phase['from']) * frac

    def _view(self, job):
        if job is None:
            return None
        out = {k: v for k, v in job.items() if not k.startswith('_')}
        out['steps'] = [{k: v for k, v in s.items() if not k.startswith('_')} for s in job['steps']]
        out['targets'] = dict(job['targets'])
        return out

    def job(self, job_id):
        with self._lock:
            return self._view(self._jobs.get(job_id))

    def jobs(self):
        with self._lock:
            return [self._view(j) for j in reversed(self._jobs.values())]

    def current(self):
        """The newest job, running or finished (None before the first one)."""
        with self._lock:
            return self._view(self._jobs.get(self._current))

    def abort(self, job_id=None):
        """Stop after the axis moving now (the firmware has no stop command); a manual
        simulated job, where nothing moves, stops at once. Returns the job, or None when
        nothing was running."""
        with self._lock:
            job = self._jobs.get(job_id or self._current)
            if not job or job['state'] not in ACTIVE:
                return None
            if job['mode'] == 'manual':
                self._finish(job, 'aborted', skip_from=0)
            else:
                job['state'] = 'aborting'
            return self._view(job)

    # ----- simulation -----

    def set_simulation(self, on=None, mode=None, speed=None):
        """Change the simulation settings. Switching on / off is refused while a job runs."""
        try:
            Simulation.check(on, mode, speed)
        except SimulationError as e:
            raise RoomError(str(e), 400) from e
        with self._lock:
            job = self._jobs.get(self._current)
            if on is not None and on != self.sim.on and job and job['state'] in ACTIVE:
                raise RoomError(f'Finish or stop "{job["name"]}" first', 409)
            self.sim.update(on=on, mode=mode, speed=speed)
            return self.sim.settings()

    def waiting_job(self):
        """The manual simulated job waiting for someone to mark it done, or None."""
        with self._lock:
            job = self._jobs.get(self._current)
            if job and job['mode'] == 'manual' and job['state'] == 'running':
                return self._view(job)
            return None

    def finish_simulated(self, job_id, success, error=''):
        """Mark a waiting manual job done (axes arrive at their targets) or failed."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise RoomError('Job not found', 404)
            if job['mode'] != 'manual' or job['state'] != 'running':
                raise RoomError(f'"{job["name"]}" is not waiting for a simulated result ({job["state"]})', 409)
            now = time.time()
            if success:
                for s in job['steps']:
                    s['state'], s['started'], s['finished'] = 'done', s['started'] or now, now
                self.sim.arrive(job['targets'])
                job['index'] = len(job['steps'])
                self._finish(job, 'done')
            else:
                first = job['steps'][0]
                first['state'], first['error'] = 'failed', error or 'Simulated failure'
                job['error'] = f'{first["device"]} {first["label"]}: {first["error"]} (simulated)'
                self._finish(job, 'error', skip_from=1)
            return self._view(job)

    # ----- the 3D room -----

    def snapshot(self):
        """Every axis's position, the current job, and the devices: what the 3D room shows."""
        axes = self.config.axes()
        devices = list(OrderedDict.fromkeys(a.device for a in axes if a.wired))
        simulated = self.sim.on
        if not simulated:
            self.sync_sessions(devices)
        with self._lock:
            job = self._jobs.get(self._current)
            moving = {}
            if job and job['state'] in ACTIVE:
                moving = {s['axis']: self.estimate(s) for s in job['steps'] if s['state'] in ('homing', 'moving')}
            out_axes = {}
            for ax in axes:
                pos = self.position(ax, simulated)
                known = pos is not None
                out_axes[ax.id] = {
                    'value': round(moving.get(ax.id, ax.to_room(pos) if known else ax.default), 4),
                    'estimated': ax.id in moving or not known,
                    'known': known,
                    'homed': simulated or not ax.needs_home or self.state.homed(ax.device, ax.ch),
                    'ready': ax.ready,                  # may move on the real rig
                }
            job_view = self._view(job)
        return {'axes': out_axes, 'job': job_view, 'devices': self._device_views(devices),
                'config': self.config.info(), 'simulation': simulated}

    def _device_views(self, devices):
        out = {}
        for device_id in devices:
            device = services.store.get(device_id)
            if device is None:
                out[device_id] = {'status': 'missing', 'note': 'not in the device list'}
                continue
            out[device_id] = {
                'status': services.monitor.status(device_id),
                'name': device.get('alternative_name') or device.get('name') or device_id,
                'homed': self.state.homed_channels(device_id),
            }
        return out
