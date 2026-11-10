"""
Motion Control Center (PC side) - controls X/Y/Z rails, rotation and tilt on several
Arduinos, each behind a Raspberry Pi running the Flask service in
../pi-arduino-connection-test-0717.

Layout:
  app.py        HTTP routes + input validation (this file)
  devices.py    device list persistence (data/pi_config.json)
  monitor.py    background status polling + one-command-per-device lock
  pi_client.py  HTTP calls to the Pis
  beszel_client.py  Pi health from the Beszel hub + two-way sync of the device list
  templates/ + static/   web UI
"""

import logging
import os
import re
import time

from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

import pi_client
from beszel_client import UNLINKED, BeszelClient, BeszelError, BeszelSync
from devices import DeviceExists, DeviceStore, StoreError
from monitor import StatusMonitor

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
log = logging.getLogger('app')

# The whole data/ directory is mounted in Docker, not the single file: bind-mounting a
# file that doesn't exist yet makes Docker create a *directory* with that name.
CONFIG_FILE = os.getenv('CONFIG_FILE', os.path.join('data', 'pi_config.json'))

DEFAULT_DEVICES = {
    'pi-01': {'host': '172.18.177.105', 'port': 5000, 'name': 'pi-01'},
    'pi-02': {'host': '172.18.177.104', 'port': 5000, 'name': 'pi-02'},
}

# ----- validation rules -----
# The ID ends up in URLs and in the HTML, so keep it to safe characters.
ID_RE = re.compile(r'^[A-Za-z0-9_.-]{1,32}$')
HOST_RE = re.compile(r'^[A-Za-z0-9.-]{1,253}$')      # hostname or IPv4, no scheme/path
MAX_NAME = 64
MAX_COMMAND = 64
TIMEOUT_RANGE = (1.0, 120.0)                         # seconds a single command may take

# ----- Beszel (optional: leave BESZEL_URL empty to turn health monitoring off) -----
BESZEL_URL = os.getenv('BESZEL_URL', '')                   # how *this server* reaches the hub
BESZEL_PORT = int(os.getenv('BESZEL_PORT', '8090'))        # hub port published on the PC
# How browsers / Pis reach the hub. Empty = same host the browser used for this page.
BESZEL_PUBLIC_URL = os.getenv('BESZEL_PUBLIC_URL', '').rstrip('/')

app = Flask(__name__)
store = DeviceStore(CONFIG_FILE, DEFAULT_DEVICES)
monitor = StatusMonitor(store)
health = BeszelSync(BeszelClient(BESZEL_URL, os.getenv('BESZEL_EMAIL', ''), os.getenv('BESZEL_PASSWORD', '')), store)


# ===== helpers =====

def ok(**data):
    return jsonify({'status': 'success', **data})


def fail(message, http_status, **data):
    return jsonify({'status': 'error', 'error': message, **data}), http_status


def json_object():
    """
    The request body as a dict, or None.

    None (bad/missing JSON, or JSON that isn't an object) is reported separately from
    "valid JSON but a field is missing", so a broken client gets a message that
    points at the real problem.
    """
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else None


BAD_JSON = ('Request body must be a JSON object', 400)


def with_status(device_id, device, device_health=None):
    return {**device, 'status': monitor.status(device_id), 'running': monitor.running(device_id),
            'health': device_health}


def beszel_public_url():
    """Hub URL as seen from the browser's / the Pis' side of the network."""
    if BESZEL_PUBLIC_URL:
        return BESZEL_PUBLIC_URL
    return f'{request.scheme}://{request.host.rsplit(":", 1)[0]}:{BESZEL_PORT}'


def validate_device_fields(data):
    """Returns (fields, error_message)."""
    host = str(data.get('host', '')).strip()
    if not HOST_RE.match(host):
        return None, 'Host must be a hostname or IPv4 address (no http://, no port)'
    try:
        port = int(data.get('port', 5000))
    except (TypeError, ValueError):
        return None, f"Invalid port: {data.get('port')!r}"
    if not 1 <= port <= 65535:
        return None, f'Port out of range: {port}'
    name = str(data.get('name') or '').strip()
    alt = str(data.get('alternative_name') or '').strip()
    if len(name) > MAX_NAME or len(alt) > MAX_NAME:
        return None, f'Names are limited to {MAX_NAME} characters'
    return {'host': host, 'port': port, 'name': name, 'alternative_name': alt}, None


# Appended to static URLs so browsers fetch new JS/CSS after a restart/rebuild
# instead of running a cached copy against a changed API.
ASSET_VERSION = str(int(time.time()))


@app.context_processor
def asset_version():
    return {'v': ASSET_VERSION}


@app.before_request
def start_monitor():
    monitor.start()
    health.start()


@app.errorhandler(HTTPException)
def http_error(e):
    # API callers always get JSON, never Flask's HTML error page
    if request.path.startswith('/api/'):
        return fail(e.description, e.code)
    return e


@app.errorhandler(Exception)
def unexpected_error(e):
    log.exception('Unhandled error on %s %s', request.method, request.path)
    return fail('Internal server error (see server log)', 500)


@app.errorhandler(StoreError)
def store_error(e):
    return fail(f'Could not save the device list: {e}', 500)


# ===== pages =====

@app.route('/')
def index():
    # No probing here: the page loads instantly and fetches /api/pis itself.
    return render_template('index.html')


# ===== API =====

@app.route('/api/pis', methods=['GET'])
def list_pis():
    """All devices with their last known status (from memory, never blocks)."""
    monitor.touch()
    devices = store.all()
    hp = health.health_by_device(devices)
    return ok(pis={i: with_status(i, d, hp.get(i)) for i, d in devices.items()},
              beszel=beszel_summary(devices))


def beszel_summary(devices):
    """Hub state + Beszel systems that belong to no device (for "Add as device" / "Link")."""
    configured = health.client.configured
    return {**health.state(),
            'url': beszel_public_url() if configured else None,
            'unlinked': health.unlinked(devices) if configured else []}


@app.route('/api/beszel', methods=['GET'])
def beszel_state():
    """Is health monitoring on, and can we reach the hub?"""
    return ok(beszel=beszel_summary(store.all()))


@app.route('/api/pi/<pi_id>/beszel', methods=['POST'])
def link_beszel(pi_id):
    """
    Link a device to a Beszel system.
      {"system_id": "<id>"}  link to that system (taken away from any other device)
      {"system_id": null}    unlink and never auto-match again
      {"system_id": ""}      unlink and allow auto-matching by name again
    """
    data = json_object()
    if data is None or 'system_id' not in data:
        return fail('Body must be {"system_id": "<id>" | null | ""}', 400)
    if store.get(pi_id) is None:
        return fail('Pi not found', 404)

    sid = data['system_id']
    if sid is None:
        sid = UNLINKED
    elif not isinstance(sid, str):
        return fail('system_id must be a string or null', 400)
    elif sid and health.system(sid) is None:
        return fail('Unknown Beszel system (it may have just been deleted)', 404)

    if sid not in ('', UNLINKED):
        # One system belongs to one device: release it from whoever had it.
        for other_id, other in store.all().items():
            if other_id != pi_id and other['beszel_system_id'] == sid:
                store.update(other_id, beszel_system_id=UNLINKED, beszel_name='')

    # beszel_name='' makes the next sync push this device's name to the system.
    device = store.update(pi_id, beszel_system_id=sid, beszel_name='')
    health.wake()
    return ok(pi_id=pi_id, pi=with_status(pi_id, device))


@app.route('/api/beszel/agent-env', methods=['GET'])
def beszel_agent_env():
    """
    The lines to paste into each Pi's .env so its agent registers with our hub.
    ⚠ Contains the registration token: anyone with it can add systems to the hub.
    (This service has no login at all, so it is no more exposed than the controls.)
    """
    if not health.client.configured:
        return fail('Beszel is not configured (BESZEL_URL is empty)', 400)
    try:
        settings = health.client.agent_settings()
    except BeszelError as e:
        return fail(str(e), 502)

    hub_url = beszel_public_url()
    warning = None
    if re.search(r'//(localhost|127\.|\[?::1)', hub_url):
        warning = ('This page was opened via localhost, so HUB_URL points to the Pi itself. '
                   "Open this page using the PC's LAN IP, or set BESZEL_PUBLIC_URL in .env.")
    env = '\n'.join([
        'COMPOSE_PROFILES=monitoring',
        f'BESZEL_HUB_URL={hub_url}',
        f'BESZEL_KEY="{settings["key"]}"',
        f'BESZEL_TOKEN={settings["token"]}',
    ])
    return ok(env=env, hub_url=hub_url, warning=warning)


@app.route('/api/pi/<pi_id>', methods=['GET'])
def get_pi(pi_id):
    """One device, probed right now (blocks up to STATUS_TIMEOUT)."""
    device = store.get(pi_id)
    if device is None:
        return fail('Pi not found', 404)
    if monitor.running(pi_id) is None:
        monitor.set(pi_id, pi_client.probe(device))
    return ok(pi=with_status(pi_id, device))


@app.route('/api/pi', methods=['POST'])
def add_pi():
    data = json_object()
    if data is None:
        return fail(*BAD_JSON)

    pi_id = str(data.get('pi_id', '')).strip()
    if not ID_RE.match(pi_id):
        return fail('Device ID: 1-32 characters, letters, digits, "-", "_" or "." only', 400)

    fields, error = validate_device_fields(data)
    if error:
        return fail(error, 400)

    # Optional: link straight to a Beszel system ("Add as device" from an unlinked agent).
    sid = str(data.get('beszel_system_id') or '').strip()
    if sid:
        if health.system(sid) is None:
            return fail('Unknown Beszel system (it may have just been deleted)', 400)
        if any(d['beszel_system_id'] == sid for d in store.all().values()):
            return fail('That Beszel system is already linked to another device', 400)
        fields['beszel_system_id'] = sid

    try:
        device = store.add(pi_id, fields)
    except DeviceExists:
        return fail(f"Pi '{pi_id}' already exists", 400)

    # Don't make the user wait for the probe; the UI shows "Checking" until it lands.
    monitor.check_soon(pi_id)
    health.wake()
    return ok(message=f"Pi '{pi_id}' added", pi_id=pi_id, pi=with_status(pi_id, device))


@app.route('/api/pi/<pi_id>', methods=['PATCH'])
def update_pi(pi_id):
    """Change alternative_name. An empty string clears it (the UI falls back to name)."""
    data = json_object()
    if data is None:
        return fail(*BAD_JSON)
    if 'alternative_name' not in data:
        return fail('Missing alternative_name', 400)

    alt = str(data.get('alternative_name') or '').strip()
    if len(alt) > MAX_NAME:
        return fail(f'alternative_name is limited to {MAX_NAME} characters', 400)

    device = store.update(pi_id, alternative_name=alt)
    if device is None:
        return fail('Pi not found', 404)
    health.wake()   # push the new name to the linked Beszel system right away
    return ok(pi_id=pi_id, pi=with_status(pi_id, device))


@app.route('/api/pi/<pi_id>', methods=['DELETE'])
def delete_pi(pi_id):
    """
    Remove a device. ?beszel=1 also deletes its linked Beszel system (and its history).
    ⚠ If that Pi's agent is still running it registers again a few seconds later and
    shows up under "Unlinked agents"; stop the agent on the Pi to remove it for good.
    """
    if monitor.running(pi_id):
        return fail('A command is still running on this device', 409)
    device = store.get(pi_id)
    if device is None:
        return fail('Pi not found', 404)

    beszel_deleted = False
    sid = device['beszel_system_id']
    if request.args.get('beszel') == '1' and sid not in ('', UNLINKED):
        # Beszel first: if it fails, nothing is deleted and the user can retry or untick.
        try:
            health.client.delete_system(sid)
            beszel_deleted = True
        except BeszelError as e:
            return fail(f'Device not removed: could not delete it from Beszel ({e})', 502)

    store.remove(pi_id)
    monitor.forget(pi_id)
    health.wake()
    return ok(message=f"Pi '{pi_id}' deleted", beszel_deleted=beszel_deleted)


@app.route('/api/pi/<pi_id>/test', methods=['GET', 'POST'])
def test_pi(pi_id):
    """Ask the Arduino to answer once. Response always carries pi_status."""
    device = store.get(pi_id)
    if device is None:
        return fail('Pi not found', 404)
    if monitor.running(pi_id):
        # The serial port is taken; a ping would just wait and time out.
        return fail(f'Busy running {monitor.running(pi_id)}', 409, pi_status=monitor.status(pi_id))

    try:
        body = pi_client.ping(device)
    except pi_client.PiError as e:
        monitor.set(pi_id, e.pi_status)
        return fail(str(e), e.http_status, pi_status=monitor.status(pi_id))

    monitor.set(pi_id, pi_client.ONLINE)
    return ok(pi_id=pi_id, pi_status=pi_client.ONLINE, message='Connection successful', pi_response=body)


@app.route('/api/pi/<pi_id>/command', methods=['POST'])
def send_command(pi_id):
    """
    Send one firmware command, e.g. {"command": "SXM,500", "timeout": 40}.
    The optional timeout (seconds) is forwarded to the Pi.
    """
    device = store.get(pi_id)
    if device is None:
        return fail('Pi not found', 404)

    data = json_object()
    if data is None:
        return fail(*BAD_JSON)

    cmd = str(data.get('command', '')).strip()
    if not cmd:
        return fail('Missing command', 400)
    if len(cmd) > MAX_COMMAND or any(c in cmd for c in '\r\n'):
        # ⚠ The Pi appends "\n" and writes the string as-is: an embedded newline would
        # send two commands while the Pi waits for only one DONE, desyncing the link.
        return fail(f'Command must be one line, at most {MAX_COMMAND} characters', 400)

    timeout = None
    if data.get('timeout') not in (None, ''):
        try:
            timeout = float(data['timeout'])
        except (TypeError, ValueError):
            return fail(f"Invalid timeout value: {data['timeout']!r}", 400)
        lo, hi = TIMEOUT_RANGE
        if not lo <= timeout <= hi:
            return fail(f'Timeout must be between {lo:g} and {hi:g} seconds', 400)

    # One command per device: the Arduino handles one blocking command at a time, and a
    # second one (another tab, "Home all" overlapping a click) would only queue up on
    # the Pi's serial lock and likely time out.
    if not monitor.try_begin(pi_id, cmd):
        return fail(f'Busy running {monitor.running(pi_id)}', 409, busy=True)
    try:
        body = pi_client.send_command(device, cmd, timeout)
    except pi_client.PiError as e:
        monitor.set(pi_id, e.pi_status)
        return fail(str(e), e.http_status, pi_status=monitor.status(pi_id))
    finally:
        monitor.end(pi_id)

    monitor.set(pi_id, pi_client.ONLINE)
    return ok(pi_id=pi_id, command=cmd, pi_response=body)


if __name__ == '__main__':
    # debug=True exposes the Werkzeug debugger to the whole LAN (remote code execution),
    # so it is off unless FLASK_DEBUG=1 is set for local development.
    debug = os.getenv('FLASK_DEBUG', '0').lower() in ('1', 'true', 'yes')
    log.info('Starting Motion Control Center on :5001 (debug=%s)', debug)
    # threaded=True: a 40 s calibrate on one device must not block requests for others.
    app.run(host='0.0.0.0', port=5001, debug=debug, threaded=True)
