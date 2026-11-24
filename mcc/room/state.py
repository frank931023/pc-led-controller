"""
What the control center remembers between commands and restarts:

  RoomState   per device: the Arduino's boot session, homed channels, last position sent
              (data/room_state.json). Real hardware only.
  Simulation  the simulation switch, how simulated jobs run, and where every axis "is"
              while simulating (data/simulation.json). Kept apart on purpose: pretend
              positions must never be mistaken for real ones once the Pis are connected.
"""

import json
import logging
import math
import threading

from ..storage import write_json_atomic
from .axes import CALIBRATE_RE, HOME_RE, HOMING_CHANNELS, MOVE_RE

log = logging.getLogger('mcc.room')


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


class SimulationError(ValueError):
    """Bad simulation settings (the message says which and why)."""


class Simulation:
    """
    The simulation switch and settings, plus where each axis "is" while simulating
    (room units, by axis id).

      on      jobs send nothing to the Pis
      mode    "auto":   a job plays out by itself, axis by axis, at `speed` times the
                        nominal speed (progress, 3D animation, done at the end)
              "manual": a job waits until someone marks it done / failed on the page
      speed   1 ... 100, auto mode only

    A fault can be injected for the next auto job (test scripts use it): that job fails
    at the given step with the given message, as a real firmware error would.
    """

    MODES = ('auto', 'manual')
    SPEED_MIN, SPEED_MAX = 1.0, 100.0

    def __init__(self, path):
        self._path = path
        self._lock = threading.Lock()
        self.on = False
        self.mode = 'manual'
        self.speed = 1.0
        self._axes = {}
        self._fault = None
        if path:
            self._load()

    def _load(self):
        try:
            with open(self._path, encoding='utf-8') as f:
                data = json.load(f)
            self._axes = {str(k): float(v) for k, v in (data.get('axes') or {}).items()}
            self.update(on=data.get('on', False), mode=data.get('mode', 'manual'),
                        speed=data.get('speed', 1.0), save=False)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, AttributeError) as e:
            log.warning('Ignoring unreadable %s (%s)', self._path, e)

    def _save(self):
        if not self._path:
            return
        data = {'on': self.on, 'mode': self.mode, 'speed': self.speed, 'axes': self._axes}
        try:
            write_json_atomic(self._path, data)
        except OSError as e:
            log.warning('Could not save %s: %s', self._path, e)

    # ----- settings -----

    def settings(self):
        with self._lock:
            return {'on': self.on, 'mode': self.mode, 'speed': self.speed}

    @classmethod
    def check(cls, on=None, mode=None, speed=None):
        """Validated (on, mode, speed); None = leave unchanged. Raises SimulationError."""
        if on is not None and not isinstance(on, bool):
            raise SimulationError('"on" must be true or false')
        if mode is not None and mode not in cls.MODES:
            raise SimulationError(f'"mode" must be one of {", ".join(cls.MODES)}')
        if speed is not None:
            if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not math.isfinite(speed):
                raise SimulationError('"speed" must be a number')
            if not cls.SPEED_MIN <= speed <= cls.SPEED_MAX:
                raise SimulationError(f'"speed" must be between {cls.SPEED_MIN:g} and {cls.SPEED_MAX:g}')
            speed = float(speed)
        return on, mode, speed

    def update(self, on=None, mode=None, speed=None, save=True):
        on, mode, speed = self.check(on, mode, speed)
        with self._lock:
            if on is not None:
                self.on = on
            if mode is not None:
                self.mode = mode
            if speed is not None:
                self.speed = speed
            if save:
                self._save()

    # ----- pretend positions -----

    def position(self, axis_id):
        with self._lock:
            return self._axes.get(axis_id)

    def arrive(self, targets):
        """Axes reached these positions ({axis id: room value})."""
        with self._lock:
            self._axes.update(targets)
            self._save()

    def forget(self, axis_id):
        """A simulated move failed: where the axis stopped is not known."""
        with self._lock:
            self._axes.pop(axis_id, None)
            self._save()

    # ----- fault injection (next auto job only) -----

    def inject_fault(self, step, error):
        if isinstance(step, bool) or not isinstance(step, int) or step < 0:
            raise SimulationError('fault step must be an integer >= 0')
        with self._lock:
            self._fault = {'step': step, 'error': str(error) or 'Simulated failure'}

    def take_fault(self):
        """The injected fault, once (None when there is none)."""
        with self._lock:
            fault, self._fault = self._fault, None
            return fault
