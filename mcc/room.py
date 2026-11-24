"""
The room: every movable axis, where each one is, and setup jobs that move them.

A setup is "every object in the room at a given position": {axis id: target} in room
units (metres / degrees), e.g. {"hats.mrp": 1.20, "hats.rot": 90}. ACQUA Automation and
the 3D room both send setups here; this module decides how to get there:

  RoomConfig  the axis table (mcc/room.json, or data/room.json when it exists): which Pi
              and firmware axis each axis id is, its travel, and the move order
  RoomState   per device: the Arduino's boot session, which axes are homed, and where
              each axis was last sent (data/room_state.json, survives restarts)
  Room        validates a setup, then runs it as a background job, one axis at a time

Why homing is tracked
---------------------
The firmware counts steps from wherever it was at power-on, so an absolute move is only
right after that axis has been homed (driven to its home switch) since the Arduino last
restarted. Opening the serial port resets a Mega, so a Pi reboot, a redeploy or a USB
reconnect all make the Arduino forget every position, silently. The Pi reports an
`arduino_session` that changes on every restart; when it changes, every axis on that
device counts as not homed, and the next setup homes it before moving it.

Positions are what we last *sent*, not measured: the steppers are open loop, and asking
the firmware (SXP...) would just echo its own step count back.

Simulation (no Pis yet)
-----------------------
With simulation on, a job sends nothing: it waits until someone on the web page marks it
done (or failed), then the axes "are" at their targets. ACQUA polls the same job and
cannot tell the difference, so the whole ACQUA -> MCC path can be tried without hardware.
"""

import json
import logging
import math
import os
import re
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass

from . import pi_client, services
from .dispatch import CommandError, resolve_timeout, run
from .storage import write_json_atomic
from .web import ID_RE

log = logging.getLogger('mcc.room')

HOMING_CHANNELS = ('SX', 'SY', 'SZ', 'R')      # driven to a home switch: <ch>H
CHANNELS = HOMING_CHANNELS + ('T',)            # T is a servo: absolute, nothing to home
UNITS = ('m', 'deg')

HOME_RE = re.compile(r'^(SX|SY|SZ|R)H$')
CALIBRATE_RE = re.compile(r'^(SX|SY|SZ|R)C$')
MOVE_RE = re.compile(r'^(SX|SY|SZ|R|T)M,(-?\d+)$')

BUSY_WAIT = 30          # seconds a job waits for a manual command that is still running
KEEP_JOBS = 20          # finished jobs kept for polling (memory only)


# ════════════════════════════════════════════════════════════════════════════
#  Axis table
# ════════════════════════════════════════════════════════════════════════════

class RoomConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Axis:
    id: str
    device: str | None      # None: not wired to a Pi yet (3D room / simulation only)
    ch: str | None
    min: float
    max: float
    unit: str
    label: str
    speed: float
    default: float
    # True once someone checked on the real rig that device + ch drive this axis and that
    # min / max match the calibrated travel. Until then the axis never moves for real.
    verified: bool = False
    note: str = ''

    @property
    def wired(self):
        return self.device is not None

    @property
    def needs_home(self):
        return self.ch in HOMING_CHANNELS

    def to_device(self, value):
        """Room units -> firmware value: mm from the home end (linear) or degrees."""
        if self.unit == 'deg':
            return int(round(value))
        return int(round((value - self.min) * 1000))

    def to_room(self, device_value):
        if self.unit == 'deg':
            return float(device_value)
        return self.min + device_value / 1000.0

    def move_command(self, value):
        if not self.wired:
            return f'(not wired: {self.to_device(value)})'
        return f'{self.ch}M,{self.to_device(value)}'

    @property
    def home_command(self):
        return f'{self.ch}H'

    def seconds(self, a, b):
        return abs(b - a) / self.speed

    def view(self):
        return {'id': self.id, 'device': self.device, 'ch': self.ch, 'label': self.label,
                'unit': self.unit, 'min': self.min, 'max': self.max,
                'speed': self.speed, 'default': self.default,
                'wired': self.wired, 'verified': self.verified, 'note': self.note}

    @property
    def ready(self):
        """May move on the real rig: wired to a Pi and verified there (room.json)."""
        return self.wired and self.verified


def parse_axes(data):
    """The axes of a room.json document, validated. Raises RoomConfigError."""
    rows = data.get('axes') if isinstance(data, dict) else None
    if not isinstance(rows, list) or not rows:
        raise RoomConfigError('needs a non-empty "axes" list')
    axes, errors, ids, wiring = [], [], set(), {}
    for i, row in enumerate(rows):
        where = f'axes[{i}]'
        if not isinstance(row, dict):
            errors.append(f'{where}: not an object')
            continue
        aid = str(row.get('id') or '')
        where = f'{where} ({aid or "no id"})'
        try:
            low, high = float(row['min']), float(row['max'])
            speed = float(row.get('speed', 0.07))
            default = float(row.get('default', low))
        except (KeyError, TypeError, ValueError):
            errors.append(f'{where}: min / max / speed / default must be numbers')
            continue
        problems = []
        device, ch = row.get('device'), row.get('ch')
        if not aid or aid in ids:
            problems.append('id missing or repeated')
        if device is None:
            if ch is not None:
                problems.append('ch is set but device is null')
            if row.get('verified'):
                problems.append('verified needs a device and ch')
        else:
            if not ID_RE.match(str(device)):
                problems.append('bad device id')
            if ch not in CHANNELS:
                problems.append(f'ch must be one of {", ".join(CHANNELS)}')
        if not isinstance(row.get('verified', False), bool):
            problems.append('verified must be true or false')
        if row.get('unit') not in UNITS:
            problems.append('unit must be m or deg')
        if not low < high:
            problems.append('min must be below max')
        if speed <= 0:
            problems.append('speed must be above 0')
        key = (device, ch)
        if device is not None and key in wiring:
            problems.append(f'{device} {ch} is already used by {wiring[key]}')
        if problems:
            errors.append(f'{where}: {"; ".join(problems)}')
            continue
        ids.add(aid)
        if device is not None:
            wiring[key] = aid
        axes.append(Axis(aid, device, ch, low, high, row['unit'],
                         str(row.get('label') or aid), speed, min(high, max(low, default)),
                         verified=bool(row.get('verified', False)), note=str(row.get('note') or '')))
    if errors:
        raise RoomConfigError('; '.join(errors))
    return axes


class RoomConfig:
    """
    The axis table, reloaded when its file changes. The first existing path wins
    (data/room.json overrides the bundled mcc/room.json). A broken edit keeps the last
    good table and reports the error instead of taking the room down.
    """

    def __init__(self, paths):
        self._paths = list(paths)
        self._lock = threading.Lock()
        self._axes = []
        self._error = None
        self._signature = None
        self._path = None

    def _reload(self):
        path = next((p for p in self._paths if os.path.isfile(p)), None)
        if path is None:
            self._error, self._signature = f'No axis table at {" or ".join(self._paths)}', None
            return
        try:
            signature = (path, os.path.getmtime(path))
        except OSError:
            return
        if signature == self._signature:
            return
        self._signature, self._path = signature, path
        try:
            with open(path, encoding='utf-8') as f:
                self._axes = parse_axes(json.load(f))
            self._error = None
        except (OSError, ValueError) as e:
            self._error = f'{path}: {e}'
            log.error('Axis table not loaded (%s); keeping the previous one', self._error)

    def axes(self):
        with self._lock:
            self._reload()
            return list(self._axes)

    def info(self):
        with self._lock:
            self._reload()
            return {'path': self._path, 'error': self._error}


# ════════════════════════════════════════════════════════════════════════════
#  Firmware answers
# ════════════════════════════════════════════════════════════════════════════

def firmware_failure(command, body):
    """
    Why a command that came back from the Pi still did not do its job, or None.

    The firmware ends almost everything with DONE, failures included: "Exceeded Max
    travel." (target beyond the calibrated travel, or never calibrated) and
    "ERROR: limit switch timeout ..." both still finish with DONE.
    """
    if body.get('arduino_result') == 'ERR':
        return f'The firmware does not know {command}'
    for line in body.get('arduino_response') or []:
        text = str(line)
        if 'Exceeded Max travel' in text:
            return (f'{command}: beyond the calibrated travel '
                    '(calibrate this axis first, or check the target)')
        if text.startswith('ERROR'):
            return f'{command}: {text}'
    return None


# ════════════════════════════════════════════════════════════════════════════
#  Homed state and positions
# ════════════════════════════════════════════════════════════════════════════

class RoomState:
    """
    Per device: current Arduino session, homed channels, last position sent per channel
    (firmware units, keyed by channel so renaming axes in room.json loses nothing).
    """

    def __init__(self, path):
        self._path = path
        self._lock = threading.Lock()
        self._devices = {}
        try:
            with open(path, encoding='utf-8') as f:
                for device_id, d in (json.load(f).get('devices') or {}).items():
                    self._devices[device_id] = {
                        'session': d.get('session'), 'seen': list(d.get('seen') or []),
                        'homed': set(d.get('homed') or []),
                        'pos': {ch: int(v) for ch, v in (d.get('pos') or {}).items()}}
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, AttributeError) as e:
            log.warning('Ignoring unreadable %s (%s): every axis starts as not homed', path, e)

    def _entry(self, device_id):
        return self._devices.setdefault(device_id, {'session': None, 'seen': [], 'homed': set(), 'pos': {}})

    def _save(self):
        data = {'devices': {d: {'session': e['session'], 'seen': e['seen'][-20:],
                                'homed': sorted(e['homed']), 'pos': e['pos']}
                            for d, e in self._devices.items()}}
        try:
            write_json_atomic(self._path, data)
        except OSError as e:
            log.warning('Could not save %s: %s', self._path, e)

    def note_session(self, device_id, session):
        """
        Record the Arduino session a device reports. A session never seen before means
        the Arduino restarted: nothing on it is homed any more. Returns True on a restart.

        Remembering past sessions matters: the status poll can still carry the previous
        session after a command already brought the new one, and going back to it must
        not count as yet another restart.
        """
        if not session:
            return False
        with self._lock:
            e = self._entry(device_id)
            if session == e['session'] or session in e['seen']:
                return False
            e['seen'] = (e['seen'] + [session])[-20:]
            restarted = e['session'] is not None
            e['session'] = session
            e['homed'].clear()
            e['pos'].clear()
            self._save()
            return restarted

    def session(self, device_id):
        with self._lock:
            return self._entry(device_id)['session']

    def homed(self, device_id, ch):
        """Homed since the Arduino last restarted. A Pi that never reported a session
        (too old to know about it) is never trusted: it gets homed before every move."""
        with self._lock:
            e = self._entry(device_id)
            return e['session'] is not None and ch in e['homed']

    def position(self, device_id, ch):
        """Last firmware value sent to a channel (None = unknown). Only meaningful when
        the channel is homed (or is the tilt servo)."""
        with self._lock:
            return self._entry(device_id)['pos'].get(ch)

    def homed_channels(self, device_id):
        with self._lock:
            e = self._entry(device_id)
            return sorted(e['homed']) if e['session'] else []

    def observe(self, device_id, command, body, failed):
        """Update from a command that came back from the Pi (whoever sent it)."""
        self.note_session(device_id, body.get('arduino_session'))
        with self._lock:
            e = self._entry(device_id)
            m = MOVE_RE.match(command)
            if failed:
                if m:                                   # it may have stopped anywhere
                    e['pos'].pop(m.group(1), None)
            elif HOME_RE.match(command):
                ch = command[:-1]
                e['homed'].add(ch)
                e['pos'][ch] = 0
            elif CALIBRATE_RE.match(command):
                # Calibration finds the home switch, then runs to the far end: homed, but
                # the end position in mm is not known here.
                ch = command[:-1]
                e['homed'].add(ch)
                e['pos'].pop(ch, None)
            elif m:
                ch, value = m.group(1), int(m.group(2))
                if ch in HOMING_CHANNELS and ch not in e['homed']:
                    e['pos'].pop(ch, None)              # moved relative to an unknown zero
                else:
                    e['pos'][ch] = value
            else:
                return
            self._save()


class Simulation:
    """
    Simulation switch + where each axis "is" while simulating (room units, by axis id).
    Kept apart from RoomState on purpose: pretend positions must never be mistaken for
    real ones once the Pis are connected. Saved to data/simulation.json.
    """

    def __init__(self, path):
        self._path = path
        self._lock = threading.Lock()
        self.on = False
        self._axes = {}
        if not path:
            return
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
            self.on = bool(data.get('on'))
            self._axes = {str(k): float(v) for k, v in (data.get('axes') or {}).items()}
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, AttributeError) as e:
            log.warning('Ignoring unreadable %s (%s)', path, e)

    def _save(self):
        if not self._path:
            return
        try:
            write_json_atomic(self._path, {'on': self.on, 'axes': self._axes})
        except OSError as e:
            log.warning('Could not save %s: %s', self._path, e)

    def set(self, on):
        with self._lock:
            self.on = bool(on)
            self._save()

    def position(self, axis_id):
        with self._lock:
            return self._axes.get(axis_id)

    def arrive(self, targets):
        with self._lock:
            self._axes.update(targets)
            self._save()


# ════════════════════════════════════════════════════════════════════════════
#  Setup jobs
# ════════════════════════════════════════════════════════════════════════════

class RoomError(Exception):
    """A setup was refused before anything moved. problems lists every reason."""

    def __init__(self, message, http_status, problems=()):
        super().__init__(message)
        self.http_status = http_status
        self.problems = list(problems)


class StepFailed(Exception):
    pass


ACTIVE = ('running', 'aborting')


class Room:
    def __init__(self, config, state, simulation=None):
        self.config = config
        self.state = state
        self.sim = simulation or Simulation(None)
        self._lock = threading.RLock()
        self._jobs = OrderedDict()          # id -> job (newest last)
        self._current = None                # id of the newest job

    # ----- axes -----

    def axes(self):
        return self.config.axes()

    def _by_id(self):
        return {a.id: a for a in self.config.axes()}

    def _sync_sessions(self, device_ids):
        for device_id in device_ids:
            self.state.note_session(device_id, services.monitor.info(device_id).get('arduino_session'))

    def _usable_position(self, ax, simulated=False):
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
            if ax.id in axis_ids:
                pos = self._usable_position(ax, simulated)
                if pos is not None:
                    out.setdefault(ax.device, {})[f'{ax.ch}M'] = pos
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
        Validate a setup and start moving. Everything is checked first, so a setup that
        cannot be completed is refused before a single axis moves. Raises RoomError.
        Returns the job (state "done" right away when every axis is already there).
        """
        by_id = self._by_id()
        if not by_id:
            raise RoomError(f'The axis table is not usable: {self.config.info()["error"]}', 503)
        if not isinstance(targets, dict) or not targets:
            raise RoomError('No axes to move: send {"axes": {"<axis id>": <target>, ...}}', 400)

        problems, wanted = [], {}
        for aid, raw in targets.items():
            ax = by_id.get(aid)
            if ax is None:
                problems.append(f'Unknown axis "{aid}"')
                continue
            if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
                problems.append(f'{aid}: target must be a number, got {raw!r}')
                continue
            if not ax.min <= raw <= ax.max:
                problems.append(f'{aid} ({ax.label}): {raw:g} is outside {ax.min:g} … {ax.max:g} {ax.unit}')
                continue
            wanted[aid] = float(raw)
        if problems:
            raise RoomError('The setup has invalid axes', 400, problems)

        devices = list(OrderedDict.fromkeys(by_id[aid].device for aid in by_id
                                            if aid in wanted and by_id[aid].wired))
        simulated = self.sim.on
        if not simulated:
            # an axis nobody has checked on the real rig never moves for real
            unwired = [aid for aid in wanted if not by_id[aid].wired]
            unverified = [aid for aid in wanted if by_id[aid].wired and not by_id[aid].verified]
            if unverified:
                problems.append('Not verified on the real rig yet (room.json "verified": false): '
                                + ', '.join(unverified))
            if unwired:
                problems.append('Not wired to a Pi yet (room.json "device": null): ' + ', '.join(unwired))
            if problems:
                raise RoomError('Some axes are not set up for the real rig yet', 503, problems)
        with self._lock:
            current = self._jobs.get(self._current)
            if current and current['state'] in ACTIVE:
                raise RoomError(f'Another setup is still moving: "{current["name"]}"', 409)

            if not simulated:
                # busy (someone else is using it) is worth retrying later: 409;
                # offline / missing / no Arduino is not: 503
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
                self._sync_sessions(devices)

            job = self._plan(by_id, wanted, devices, name, source, simulated)
            self._jobs[job['id']] = job
            self._current = job['id']
            self._trim()
            view = self._view(job)

        if job['state'] == 'running' and not simulated:
            threading.Thread(target=self._run, args=(job['id'],), daemon=True, name='room-job').start()
        return view

    def _plan(self, by_id, wanted, devices, name, source, simulated=False):
        """Steps in axis-table order; axes already at their target are left out."""
        steps, unchanged = [], []
        for ax in by_id.values():
            if ax.id not in wanted:
                continue
            target = wanted[ax.id]
            pos = self._usable_position(ax, simulated)
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
                '_axis': ax, '_phase': None,
            })
        now = time.time()
        return {
            'id': uuid.uuid4().hex[:12], 'name': name or 'Setup', 'source': source,
            'state': 'running' if steps else 'done', 'error': None,
            'created': now, 'started': now, 'finished': None if steps else now,
            'index': 0, 'steps': steps, 'unchanged': unchanged, 'targets': wanted,
            'devices': devices, 'eta': round(sum(s['seconds'] for s in steps), 1),
            # simulated: nothing is sent; the job waits for finish_simulated() (web page)
            'simulated': simulated,
        }

    def _trim(self):
        while len(self._jobs) > KEEP_JOBS:
            oldest = next(iter(self._jobs))
            if self._jobs[oldest]['state'] in ACTIVE:
                break
            del self._jobs[oldest]

    # ----- running -----

    def _run(self, job_id):
        job = self._jobs[job_id]
        for index, step in enumerate(job['steps']):
            with self._lock:
                if job['state'] == 'aborting':
                    for s in job['steps'][index:]:
                        s['state'] = 'skipped'
                    job['state'], job['finished'] = 'aborted', time.time()
                    return
                job['index'] = index
                step['started'] = time.time()
            try:
                self._run_step(step)
            except StepFailed as e:
                with self._lock:
                    step['state'], step['error'], step['finished'] = 'failed', str(e), time.time()
                    for s in job['steps'][index + 1:]:
                        s['state'] = 'skipped'
                    job['state'], job['error'] = 'error', f'{step["device"]} {step["label"]}: {e}'
                    job['finished'] = time.time()
                log.warning('Setup "%s" failed: %s', job['name'], job['error'])
                return
            except Exception as e:                      # a bug must not leave the job "running"
                log.exception('Setup "%s" crashed', job['name'])
                with self._lock:
                    step['state'], step['error'] = 'failed', f'Internal error: {e}'
                    job['state'], job['error'], job['finished'] = 'error', f'Internal error: {e}', time.time()
                return
            with self._lock:
                step['state'], step['finished'] = 'done', time.time()
        with self._lock:
            job['index'] = len(job['steps'])
            job['state'] = 'aborted' if job['state'] == 'aborting' else 'done'
            job['finished'] = time.time()

    def _run_step(self, step):
        ax = step['_axis']
        timeout, _ = resolve_timeout(ax.device, None)
        self._sync_sessions([ax.device])
        if ax.needs_home and not self.state.homed(ax.device, ax.ch):
            self._home(step, ax, timeout)
        session = self.state.session(ax.device)
        self._send(step, ax, step['command'], timeout, 'moving', step['to'])
        if ax.needs_home and self.state.session(ax.device) != session:
            # The Arduino restarted while moving: its zero is gone, so the move went
            # nowhere meaningful. Home and move again (once).
            self._home(step, ax, timeout)
            self._send(step, ax, step['command'], timeout, 'moving', step['to'])

    def _home(self, step, ax, timeout):
        with self._lock:
            step['homed_first'] = True
        self._send(step, ax, ax.home_command, timeout, 'homing', ax.to_room(0))

    def _send(self, step, ax, command, timeout, phase, goal):
        with self._lock:
            start = self._estimate(step, ax)
            step['state'] = phase
            step['_phase'] = {'from': start, 'to': goal, 'at': time.time()}
        try:
            body = run(ax.device, command, timeout, wait_busy=BUSY_WAIT)
        except CommandError as e:
            raise StepFailed(f'{command}: {e}') from e
        with self._lock:
            step['log'].append({'command': command, 'response': body.get('arduino_response') or []})
            step['_phase'] = None
        failure = firmware_failure(command, body)
        if failure:
            raise StepFailed(failure)
        return body

    # ----- reading -----

    def _estimate(self, step, ax):
        """Where a moving axis probably is now: linear from start to goal at its nominal
        speed. The serial line is busy until DONE, so a moving axis cannot be asked."""
        phase = step.get('_phase')
        if not phase:
            pos = self._usable_position(ax)
            return ax.to_room(pos) if pos is not None else step['from']
        total = ax.seconds(phase['from'], phase['to'])
        frac = 1.0 if total <= 0 else min(1.0, (time.time() - phase['at']) / total)
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

    def abort(self, job_id=None):
        """Stop after the axis that is moving now (the firmware has no stop command).
        Returns the job, or None when nothing was running."""
        with self._lock:
            job = self._jobs.get(job_id or self._current)
            if not job or job['state'] not in ACTIVE:
                return None
            if job['simulated']:                # nothing is moving: stop right away
                for s in job['steps']:
                    s['state'] = 'skipped'
                job['state'], job['finished'] = 'aborted', time.time()
            else:
                job['state'] = 'aborting'
            return self._view(job)

    # ----- simulation -----

    def set_simulation(self, on):
        """Switch simulation on / off. Refused while a job is running (either kind)."""
        with self._lock:
            job = self._jobs.get(self._current)
            if job and job['state'] in ACTIVE and bool(on) != self.sim.on:
                raise RoomError(f'Finish or stop "{job["name"]}" first', 409)
            self.sim.set(on)

    def waiting_job(self):
        """The simulated job waiting for someone to mark it done, or None."""
        with self._lock:
            job = self._jobs.get(self._current)
            if job and job['simulated'] and job['state'] == 'running':
                return self._view(job)
            return None

    def finish_simulated(self, job_id, success, error=''):
        """Mark a waiting simulated job done (axes arrive at their targets) or failed."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise RoomError('Job not found', 404)
            if not job['simulated'] or job['state'] != 'running':
                raise RoomError(f'"{job["name"]}" is not waiting for a simulated result ({job["state"]})', 409)
            now = time.time()
            if success:
                for s in job['steps']:
                    s['state'], s['started'], s['finished'] = 'done', s['started'] or now, now
                self.sim.arrive(job['targets'])
                job['state'], job['index'] = 'done', len(job['steps'])
            else:
                first = job['steps'][0]
                first['state'], first['error'] = 'failed', error or 'Simulated failure'
                for s in job['steps'][1:]:
                    s['state'] = 'skipped'
                job['state'] = 'error'
                job['error'] = f'{first["device"]} {first["label"]}: {first["error"]} (simulated)'
            job['finished'] = now
            return self._view(job)

    def snapshot(self):
        """Every axis's position, the current job, and the devices: what the 3D room shows."""
        axes = self.config.axes()
        devices = list(OrderedDict.fromkeys(a.device for a in axes if a.wired))
        simulated = self.sim.on
        if not simulated:
            self._sync_sessions(devices)
        with self._lock:
            job = self._jobs.get(self._current)
            moving = {}
            if job and job['state'] in ACTIVE:
                for s in job['steps']:
                    if s['state'] in ('homing', 'moving'):
                        moving[s['axis']] = self._estimate(s, s['_axis'])
            out_axes = {}
            for ax in axes:
                pos = self._usable_position(ax, simulated)
                known = pos is not None
                value = moving.get(ax.id, ax.to_room(pos) if known else ax.default)
                out_axes[ax.id] = {
                    'value': round(value, 4),
                    'estimated': ax.id in moving or not known,
                    'known': known,
                    'homed': simulated or not ax.needs_home or self.state.homed(ax.device, ax.ch),
                    'ready': ax.ready,                  # may move on the real rig
                }
            job_view = self._view(job)

        out_devices = {}
        for device_id in devices:
            device = services.store.get(device_id)
            if device is None:
                out_devices[device_id] = {'status': 'missing', 'note': 'not in the device list'}
                continue
            out_devices[device_id] = {
                'status': services.monitor.status(device_id),
                'name': device.get('alternative_name') or device.get('name') or device_id,
                'homed': self.state.homed_channels(device_id),
            }
        return {'axes': out_axes, 'job': job_view, 'devices': out_devices,
                'config': self.config.info(), 'simulation': simulated}
