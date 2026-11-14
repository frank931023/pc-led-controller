"""
Beszel hub integration (https://github.com/henrygd/beszel): Pi health + two-way sync.

Beszel = hub (PocketBase web app, runs next to this service in docker-compose) +
one agent per Pi. The agent connects *out* to the hub over WebSocket, so the hub
never needs to reach the Pis.

pi_config.json is the master device list. Each device can be linked to one Beszel
"system" (devices.py: beszel_system_id). BeszelSync keeps the two in step:

  * linking   first match by name, then remembered by system id, so renaming on
              either side never breaks the link
  * names     renamed here -> Beszel system renamed; renamed in Beszel ->
              alternative_name updated. If both changed, this side wins.
  * deletes   system deleted in Beszel -> link cleared here; device deleted here ->
              optionally deleted in Beszel too (app.py)
  * unlinked  systems that belong to no device are listed so the UI can offer
              "Add as device" / "Link"
  * history   CPU / memory / disk / temperature / load over time for the charts
              in the web UI, so nobody has to open (and log in to) Beszel itself

PocketBase REST API used:
  POST   /api/collections/users/auth-with-password
  GET    /api/collections/systems/records
  PATCH  /api/collections/systems/records/<id>      (rename)
  DELETE /api/collections/systems/records/<id>
  GET    /api/beszel/info                           (hub public key = agent KEY)
  POST   /api/beszel/universal-token                (token agents register with)
  GET    /api/collections/system_stats/records      (history for the charts)

A systems record looks like:
  { id, name, host, status: up|down|paused|pending,
    info: { cpu, mp (mem %), dp (disk %), dt (temp °C), u (uptime s), la [1,5,15], v } }

A system_stats record is one averaged sample:
  { system, type: 1m|10m|20m|120m|480m, created: "2026-01-02 03:04:05.678Z",
    stats: { cpu, mp, dp, t: {sensor: °C}, la [1,5,15], ... } }
"""

import logging
import re
import threading
from datetime import datetime, timedelta, timezone

import requests

from .devices import StoreError

log = logging.getLogger(__name__)

REQUEST_TIMEOUT = 5
UNLINKED = 'none'      # beszel_system_id value: user unlinked on purpose, never auto-match

# Chart range -> (system_stats record type, length). Beszel keeps each record type for
# about as long as the range it is used for here, the same pairs its own dashboard uses.
HISTORY_RANGES = {
    '1h': ('1m', timedelta(hours=1)),
    '12h': ('10m', timedelta(hours=12)),
    '24h': ('20m', timedelta(hours=24)),
    '1w': ('120m', timedelta(weeks=1)),
    '30d': ('480m', timedelta(days=30)),
}
SYSTEM_ID_RE = re.compile(r'^[a-z0-9]{1,32}$')   # PocketBase ids; also keeps the filter safe
PB_TIME = '%Y-%m-%d %H:%M:%S.%fZ'


class BeszelError(Exception):
    pass


class BeszelClient:
    def __init__(self, url, email, password):
        self.url = (url or '').rstrip('/')
        self._email = email
        self._password = password
        self._token = None
        self._lock = threading.Lock()

    @property
    def configured(self):
        return bool(self.url and self._email and self._password)

    # ----- auth -----

    def _login(self):
        try:
            resp = requests.post(f'{self.url}/api/collections/users/auth-with-password',
                                 json={'identity': self._email, 'password': self._password},
                                 timeout=REQUEST_TIMEOUT)
        except requests.exceptions.RequestException as e:
            raise BeszelError(f'Beszel hub not reachable ({e.__class__.__name__})') from e
        if resp.status_code != 200:
            # ⚠ USER_EMAIL/USER_PASSWORD only create the account on the hub's *first* start.
            # Changing them in .env later does not change the existing account.
            raise BeszelError('Beszel login failed: check BESZEL_EMAIL / BESZEL_PASSWORD in .env')
        self._token = resp.json().get('token')

    def _request(self, method, path, **kwargs):
        """Authenticated request; logs in on first use and once more if the token expired."""
        with self._lock:
            if not self._token:
                self._login()
            token = self._token
        for attempt in (1, 2):
            try:
                resp = requests.request(method, f'{self.url}{path}',
                                        headers={'Authorization': token},
                                        timeout=REQUEST_TIMEOUT, **kwargs)
            except requests.exceptions.RequestException as e:
                raise BeszelError(f'Beszel hub not reachable ({e.__class__.__name__})') from e
            if resp.status_code in (401, 403) and attempt == 1:
                with self._lock:
                    self._login()
                    token = self._token
                continue
            if resp.status_code == 204:
                return {}
            if resp.status_code != 200:
                raise BeszelError(f'Beszel returned HTTP {resp.status_code} for {method} {path}')
            try:
                return resp.json()
            except ValueError as e:
                raise BeszelError(f'Beszel returned non-JSON for {path}') from e
        raise BeszelError('Beszel authentication failed')

    # ----- systems -----

    def systems(self):
        body = self._request('GET', '/api/collections/systems/records',
                             params={'perPage': 500, 'fields': 'id,name,host,status,info'})
        return body.get('items', [])

    def rename_system(self, system_id, name):
        self._request('PATCH', f'/api/collections/systems/records/{system_id}', json={'name': name})

    def delete_system(self, system_id):
        self._request('DELETE', f'/api/collections/systems/records/{system_id}')

    def containers(self, system_id):
        """
        Containers currently running on one Pi, busiest first:
          [{name, image, status, health, cpu (%), memory (MB), updated (Unix s)}]
        """
        if not SYSTEM_ID_RE.match(system_id or ''):
            raise BeszelError('Invalid Beszel system id')
        body = self._request('GET', '/api/collections/containers/records', params={
            'filter': f"system='{system_id}'",
            'fields': 'name,image,status,health,cpu,memory,updated',
            'perPage': 200,
        })
        items = [{
            'name': c.get('name'),
            'image': c.get('image'),
            'status': c.get('status'),
            # Beszel stores Docker health as 0 none, 1 starting, 2 healthy, 3 unhealthy
            'health': {1: 'starting', 2: 'healthy', 3: 'unhealthy'}.get(c.get('health')),
            'cpu': c.get('cpu'),
            'memory': c.get('memory'),
            'updated': (c.get('updated') or 0) // 1000 if isinstance(c.get('updated'), (int, float)) else None,
        } for c in body.get('items', [])]
        return sorted(items, key=lambda c: -(c['cpu'] or 0))

    def history(self, system_id, range_key):
        """
        Samples for one chart range, oldest first:
          {"from", "to", "interval": seconds between samples,
           "points": [{t, cpu, mem, disk, temp, load}]}
        Times are Unix timestamps; a metric the agent does not report is None.
        from/to span the whole range, so a stretch with no samples shows as a gap.
        """
        record_type, length = HISTORY_RANGES[range_key]
        if not SYSTEM_ID_RE.match(system_id or ''):
            raise BeszelError('Invalid Beszel system id')
        now = datetime.now(timezone.utc)
        since = now - length
        body = self._request('GET', '/api/collections/system_stats/records', params={
            'filter': f"system='{system_id}' && type='{record_type}' && created>='{since.strftime(PB_TIME)}'",
            'sort': 'created',
            'fields': 'created,stats',
            'perPage': 500,      # the longest range has ~90 samples
        })
        return {'from': int(since.timestamp()), 'to': int(now.timestamp()),
                'interval': int(record_type[:-1]) * 60,
                'points': [_point(r) for r in body.get('items', [])]}

    # ----- agent setup -----

    def agent_settings(self):
        """KEY and permanent universal TOKEN that every Pi's agent needs."""
        key = self._request('GET', '/api/beszel/info').get('key')
        token_state = self._request('POST', '/api/beszel/universal-token', json={})
        if not (token_state.get('active') and token_state.get('permanent')):
            # Permanent = survives hub restarts. The default (ephemeral) one expires in 1 h,
            # after which new Pis could no longer register.
            token_state = self._request('POST', '/api/beszel/universal-token',
                                        json={'enable': 1, 'permanent': 1})
        return {'key': key, 'token': token_state.get('token')}


def display_name(device):
    """The name shown in the UI and pushed to Beszel."""
    return device.get('alternative_name') or device.get('name') or ''


class BeszelSync:
    """
    Background loop: fetch systems from the hub, reconcile them with the device list,
    and keep the latest systems in memory for /api/pis.
    """

    def __init__(self, client, store, interval=10.0):
        self.client = client
        self._store = store
        self._interval = interval
        self._lock = threading.Lock()
        self._systems = {}          # system id -> record
        self._error = None if client.configured else 'not configured'
        self._fetched = False
        self._thread = None
        self._wake = threading.Event()
        # Called as on_sync(devices, health_by_device) after every successful sync
        # (app.py hooks the alert engine in here).
        self.on_sync = None

    # ----- lifecycle -----

    def start(self):
        if not self.client.configured:
            return
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._loop, name='beszel-sync', daemon=True)
                self._thread.start()

    def wake(self):
        """Run a sync now instead of waiting for the next interval (after a local change)."""
        self._wake.set()

    def _loop(self):
        while True:
            self._wake.clear()
            try:
                self.sync_once()
            except Exception:   # never let the sync thread die
                log.exception('Beszel sync failed')
            self._wake.wait(self._interval)

    # ----- sync -----

    def sync_once(self):
        try:
            systems = {s['id']: s for s in self.client.systems()}
        except BeszelError as e:
            with self._lock:
                if self._error != str(e):
                    log.warning('%s', e)
                self._error = str(e)
            return

        try:
            self._reconcile(systems)
        except (BeszelError, StoreError) as e:
            log.warning('Beszel sync incomplete: %s', e)

        with self._lock:
            if self._error:
                log.info('Beszel hub connected (%d systems)', len(systems))
            self._systems, self._error, self._fetched = systems, None, True

        if self.on_sync:
            devices = self._store.all()
            self.on_sync(devices, self.health_by_device(devices))

    def _reconcile(self, systems):
        devices = self._store.all()
        claimed = {d['beszel_system_id'] for d in devices.values()
                   if d['beszel_system_id'] in systems}

        for device_id, d in devices.items():
            sid = d['beszel_system_id']
            if sid == UNLINKED:
                continue
            changes = {}

            # 1. linked system was deleted in Beszel -> forget the link, allow re-matching
            if sid and sid not in systems:
                log.info('%s: Beszel system %s no longer exists, unlinking', device_id, sid)
                sid = ''
                changes.update(beszel_system_id='', beszel_name='')

            # 2. not linked yet -> try to match an unclaimed system by name / host
            if not sid:
                match = _find_match(device_id, d, systems, claimed)
                if match:
                    sid = match['id']
                    claimed.add(sid)
                    changes.update(beszel_system_id=sid, beszel_name='')
                    log.info('%s: linked to Beszel system "%s" (%s)', device_id, match['name'], sid)
                    d = {**d, 'beszel_name': ''}

            # 3. names
            if sid:
                changes.update(self._sync_name(device_id, d, systems[sid]))

            if changes:
                self._store.update(device_id, **changes)

    def _sync_name(self, device_id, d, system):
        label = display_name(d)
        remote = system.get('name') or ''
        last = d.get('beszel_name') or ''

        if remote == label:
            return {'beszel_name': label} if last != label else {}

        if last and label == last:
            # Only Beszel changed -> take its name here.
            log.info('%s: renamed in Beszel "%s" -> "%s"', device_id, last, remote)
            alt = '' if remote == d.get('name') else remote
            return {'alternative_name': alt, 'beszel_name': remote}

        # Renamed here, first link, or both changed (this side wins) -> push to Beszel.
        self.client.rename_system(system['id'], label)
        system['name'] = label
        log.info('%s: Beszel system renamed "%s" -> "%s"', device_id, remote, label)
        return {'beszel_name': label}

    # ----- read side (used by app.py) -----

    def state(self):
        with self._lock:
            return {'enabled': self.client.configured,
                    'connected': self._error is None and self._fetched,
                    'error': self._error,
                    'systems': len(self._systems)}

    def system(self, system_id):
        with self._lock:
            return self._systems.get(system_id)

    def health_by_device(self, devices):
        """device_id -> health dict, or None when the device has no linked system."""
        with self._lock:
            systems = dict(self._systems)
        return {i: (_summarise(systems[d['beszel_system_id']])
                    if d.get('beszel_system_id') in systems else None)
                for i, d in devices.items()}

    def unlinked(self, devices):
        """Beszel systems that belong to no device."""
        with self._lock:
            systems = list(self._systems.values())
        linked = {d.get('beszel_system_id') for d in devices.values()}
        return [{'system_id': s['id'], 'name': s.get('name'), 'host': s.get('host'),
                 'status': s.get('status')}
                for s in systems if s['id'] not in linked]


def _find_match(device_id, device, systems, claimed):
    """
    First unclaimed system that matches, in this order:
      1. system name == device ID   (the Pi compose sets SYSTEM_NAME=${PI_ID})
      2. system name == name / alternative_name
      3. system host == device host
    ⚠ 3 is only a fallback: with Docker Desktop the hub may see the NAT gateway
    instead of the Pi's real IP.
    """
    free = [s for s in systems.values() if s['id'] not in claimed]
    names = [device_id, device.get('name'), device.get('alternative_name')]
    for wanted in names:
        if wanted:
            for s in free:
                if s.get('name') == wanted:
                    return s
    for s in free:
        if device.get('host') and s.get('host') == device['host']:
            return s
    return None


def _point(record):
    stats = record.get('stats') or {}
    temps = [v for v in (stats.get('t') or {}).values() if isinstance(v, (int, float))]
    la = stats.get('la') or []
    created = datetime.strptime(record['created'], PB_TIME).replace(tzinfo=timezone.utc)
    return {
        't': int(created.timestamp()),
        'cpu': stats.get('cpu'),
        'mem': stats.get('mp'),
        'disk': stats.get('dp'),
        'temp': max(temps) if temps else None,     # hottest sensor, like the summary tile
        'load': la[0] if la else None,
    }


def _summarise(system):
    info = system.get('info') or {}
    la = info.get('la') or []
    return {
        'system_id': system.get('id'),
        'name': system.get('name'),
        'status': system.get('status'),          # up | down | paused | pending
        'cpu': info.get('cpu'),
        'mem': info.get('mp'),
        'disk': info.get('dp'),
        'temp': info.get('dt') or None,          # 0 means "no sensor", show nothing
        'uptime': info.get('u'),
        'load': la[0] if la else None,
        'agent_version': info.get('v'),
    }
