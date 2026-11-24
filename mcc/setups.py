"""
Saved setups: named sets of axis targets, one JSON file each.

    {"id": "20261124-101530-3fa1", "name": "Near field 0.5 m", "description": "...",
     "created": "...", "updated": "...", "source": "setup" | "live" | "library",
     "axes": {"hats.mrp": 1.2, "hats.head": 17.0, ...},
     "overlays": {"points": [...], "circles": [...], "distances": [...]}}

The axes keys are the axis ids of the axis table (room.json), so a saved setup can be sent
to POST /api/room/apply as is. source tells whether the numbers were drawn in the 3D room
("setup"), read back from the rig ("live"), or ship with the code ("library").

overlays are drawing aids for the 3D room only (they never move anything): marker points
attached to objects (e.g. the DUT on the table), white dashed reference circles, and pinned
distances between objects. Their meaning lives in static/room/overlays.js.

Two places:
  data/setups/        setups saved from the 3D room (read / write)
  mcc/setup_library/  examples that ship with the code, e.g. the 14 MS Teams V5 setups
                      (read only: save a copy to change one)
"""

import json
import math
import os
import re
import secrets
import threading
from datetime import datetime

from .storage import write_json_atomic

SETUP_ID_RE = re.compile(r'^[A-Za-z0-9_.-]{1,64}$')
REF_RE = re.compile(r'^[A-Za-z0-9_.-]{1,32}$')       # ids of points, circles, objects
MAX_NAME = 120
MAX_DESCRIPTION = 4000
MAX_LABEL = 80
LIMITS = {'points': 40, 'circles': 20, 'distances': 30}
POINT_STYLES = ('dut', 'ghost', 'point')


class SetupError(ValueError):
    pass


def clean_axes(raw, axes):
    """Keep the axes the table knows, clamped to their travel. Returns (axes, dropped ids)."""
    by_id = {a.id: a for a in axes}
    out, dropped = {}, []
    for aid, value in (raw or {}).items() if isinstance(raw, dict) else []:
        ax = by_id.get(aid)
        if ax is None or isinstance(value, bool) or not isinstance(value, (int, float)):
            dropped.append(str(aid))
            continue
        out[aid] = round(min(ax.max, max(ax.min, float(value))), 4)
    return out, dropped


def _num(v, low, high, default=0.0):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return default
    return round(min(high, max(low, float(v))), 4)


def _vec(v):
    v = v if isinstance(v, list) and len(v) == 3 else [0, 0, 0]
    return [_num(x, -10, 10) for x in v]


def _ref(v):
    v = str(v or '')
    return v if REF_RE.match(v) else None


def _label(v):
    return str(v or '').strip()[:MAX_LABEL]


def clean_overlays(raw):
    """
    The overlays of a setup with every field checked (a broken item is dropped, not the setup).
    References (point "on", circle "at", distance ends) are only checked for shape here: the
    3D room knows which objects exist and skips what it cannot resolve.
    """
    if not isinstance(raw, dict):
        return {'points': [], 'circles': [], 'distances': []}
    points, circles, distances = [], [], []
    for p in (raw.get('points') or [])[:LIMITS['points']]:
        if isinstance(p, dict) and _ref(p.get('id')) and _ref(p.get('on')):
            points.append({'id': p['id'], 'label': _label(p.get('label')), 'on': p['on'],
                           'offset': _vec(p.get('offset')),
                           'style': p.get('style') if p.get('style') in POINT_STYLES else 'point'})
    for c in (raw.get('circles') or [])[:LIMITS['circles']]:
        if not (isinstance(c, dict) and _ref(c.get('id')) and _ref(c.get('at'))):
            continue
        radii = [_num(r, 0.05, 10) for r in (c.get('radii') or [])[:6]
                 if isinstance(r, (int, float)) and not isinstance(r, bool) and r > 0]
        if radii:
            circles.append({'id': c['id'], 'at': c['at'], 'offset': _vec(c.get('offset')), 'radii': radii,
                            'span': 'half' if c.get('span') == 'half' else 'full',
                            'level': 'mouth' if c.get('level') == 'mouth' else 'anchor',
                            'label': _label(c.get('label'))})
    for d in (raw.get('distances') or [])[:LIMITS['distances']]:
        if isinstance(d, dict) and _ref(d.get('from')) and _ref(d.get('to')) and d['from'] != d['to']:
            distances.append({'from': d['from'], 'to': d['to']})
    return {'points': points, 'circles': circles, 'distances': distances}


class SetupStore:
    def __init__(self, directory, library_dir=None):
        self.dir = directory
        self.library_dir = library_dir
        self._lock = threading.Lock()

    def _path(self, setup_id):
        if not SETUP_ID_RE.match(setup_id or ''):
            raise SetupError('Bad setup id')
        return os.path.join(self.dir, f'{setup_id}.json')

    @staticmethod
    def _read_dir(directory, **extra):
        out = {}
        if not directory or not os.path.isdir(directory):
            return out
        for name in sorted(os.listdir(directory)):
            if name.endswith('.json'):
                try:
                    with open(os.path.join(directory, name), encoding='utf-8') as f:
                        setup = json.load(f)
                except (OSError, ValueError):
                    continue                        # a broken file must not hide the others
                if isinstance(setup, dict) and setup.get('id'):
                    out[setup['id']] = {**setup, **extra}
        return out

    def _library(self):
        return self._read_dir(self.library_dir, readonly=True, source='library')

    def list(self):
        """Saved setups (newest change first), then the examples in file order."""
        saved = sorted(self._read_dir(self.dir).values(), key=lambda s: s.get('updated') or '', reverse=True)
        library = [s for s in self._library().values() if s['id'] not in {x['id'] for x in saved}]
        return saved + library

    def get(self, setup_id):
        try:
            with open(self._path(setup_id), encoding='utf-8') as f:
                return json.load(f)
        except SetupError:
            return None
        except (OSError, ValueError):
            return self._library().get(setup_id)

    def is_library(self, setup_id):
        return setup_id in self._library() and not os.path.isfile(self._path(setup_id))

    def save(self, data, axes, setup_id=None):
        """Create (setup_id None) or update a setup. Raises SetupError / OSError."""
        name = str(data.get('name') or '').strip()
        if not name:
            raise SetupError('A setup needs a name')
        if len(name) > MAX_NAME:
            raise SetupError(f'Name: at most {MAX_NAME} characters')
        description = str(data.get('description') or '').strip()[:MAX_DESCRIPTION]
        values, dropped = clean_axes(data.get('axes'), axes)
        if not values:
            raise SetupError('A setup needs at least one known axis')

        now = datetime.now().isoformat(timespec='seconds')
        with self._lock:
            if setup_id and self.is_library(setup_id):
                raise SetupError('Examples are read only: save a copy instead')
            existing = self.get(setup_id) if setup_id else None
            if setup_id and existing is None:
                raise SetupError('Setup not found')
            setup = {
                'id': setup_id or f'{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}',
                'name': name, 'description': description,
                'created': existing['created'] if existing else now, 'updated': now,
                'source': data.get('source') if data.get('source') in ('setup', 'live') else 'setup',
                'axes': values,
                'overlays': clean_overlays(data.get('overlays')),
            }
            write_json_atomic(self._path(setup['id']), setup)
        return setup, dropped

    def delete(self, setup_id):
        """True when deleted. Examples cannot be deleted (raises SetupError)."""
        with self._lock:
            if self.is_library(setup_id):
                raise SetupError('Examples are read only')
            try:
                os.remove(self._path(setup_id))
                return True
            except (OSError, SetupError):
                return False
