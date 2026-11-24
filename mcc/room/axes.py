"""
The axis table: every movable axis of the room, which Pi and firmware axis drives it,
its travel, and how to read the firmware's answers.
"""

import json
import logging
import os
import re
import threading
from dataclasses import dataclass

from ..web import ID_RE

log = logging.getLogger('mcc.room')

HOMING_CHANNELS = ('SX', 'SY', 'SZ', 'R')      # driven to a home switch: <ch>H
CHANNELS = HOMING_CHANNELS + ('T',)            # T is a servo: absolute, nothing to home
UNITS = ('m', 'deg')

HOME_RE = re.compile(r'^(SX|SY|SZ|R)H$')
CALIBRATE_RE = re.compile(r'^(SX|SY|SZ|R)C$')
MOVE_RE = re.compile(r'^(SX|SY|SZ|R|T)M,(-?\d+)$')


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
    def ready(self):
        """May move on the real rig: wired to a Pi and verified there (room.json)."""
        return self.wired and self.verified

    @property
    def needs_home(self):
        return self.ch in HOMING_CHANNELS

    @property
    def move_key(self):
        """The firmware's absolute-move command name, e.g. "SZM" (what ACQUA sends)."""
        return f'{self.ch}M'

    def to_device(self, value):
        """Room units -> firmware value: mm from the home end (linear) or degrees."""
        if self.unit == 'deg':
            return int(round(value))
        return int(round((value - self.min) * 1000))

    def to_room(self, device_value):
        if self.unit == 'deg':
            return float(device_value)
        return self.min + device_value / 1000.0

    def device_travel(self):
        """(low, high) of the travel in firmware units."""
        if self.unit == 'm':
            return 0, self.to_device(self.max)
        return int(round(self.min)), int(round(self.max))

    def move_command(self, value):
        if not self.wired:
            return f'(not wired: {self.to_device(value)})'
        return f'{self.move_key},{self.to_device(value)}'

    @property
    def home_command(self):
        return f'{self.ch}H'

    def seconds(self, a, b):
        """Nominal time to move from a to b (room units) on the real rig."""
        return abs(b - a) / self.speed

    def view(self):
        return {'id': self.id, 'device': self.device, 'ch': self.ch, 'label': self.label,
                'unit': self.unit, 'min': self.min, 'max': self.max,
                'speed': self.speed, 'default': self.default,
                'wired': self.wired, 'verified': self.verified, 'note': self.note}


def _row_problems(row, aid, ids, wiring, low, high, speed):
    """Everything wrong with one row of the table (empty list = fine)."""
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
        if (device, ch) in wiring:
            problems.append(f'{device} {ch} is already used by {wiring[(device, ch)]}')
    if not isinstance(row.get('verified', False), bool):
        problems.append('verified must be true or false')
    if row.get('unit') not in UNITS:
        problems.append('unit must be m or deg')
    if not low < high:
        problems.append('min must be below max')
    if speed <= 0:
        problems.append('speed must be above 0')
    return problems


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
        problems = _row_problems(row, aid, ids, wiring, low, high, speed)
        if problems:
            errors.append(f'{where}: {"; ".join(problems)}')
            continue
        ids.add(aid)
        if row.get('device') is not None:
            wiring[(row['device'], row['ch'])] = aid
        axes.append(Axis(aid, row.get('device'), row.get('ch'), low, high, row['unit'],
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
