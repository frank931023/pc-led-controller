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

PocketBase REST API used:
  POST   /api/collections/users/auth-with-password
  GET    /api/collections/systems/records
  PATCH  /api/collections/systems/records/<id>      (rename)
  DELETE /api/collections/systems/records/<id>
  GET    /api/beszel/info                           (hub public key = agent KEY)
  POST   /api/beszel/universal-token                (token agents register with)

A systems record looks like:
  { id, name, host, status: up|down|paused|pending,
    info: { cpu, mp (mem %), dp (disk %), dt (temp °C), u (uptime s), la [1,5,15], v } }
"""

import logging
import threading
import time

import requests

from devices import StoreError

log = logging.getLogger(__name__)

REQUEST_TIMEOUT = 5
UNLINKED = 'none'      # beszel_system_id value: user unlinked on purpose, never auto-match


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
