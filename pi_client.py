"""
HTTP calls from the PC to a Pi's Flask service (see ../pi-arduino-connection-test-0717).

Pi API contract this module relies on:
  GET  /api/status    always 200 while Flask is up;
                      arduino_status = connected | disconnected | busy (+ running),
                      plus command_timeout / max_command_timeout / app_version
  GET  /api/ping      200 if the Arduino answered, 503 serial problem, 409 busy
  GET  /api/commands  firmware command catalog with every timeout filled in
  POST /api/command   200 with arduino_result DONE|ERR, 400 bad request,
                      409 another command is running, 503 serial problem
"""

import logging
from dataclasses import dataclass, field

import requests

log = logging.getLogger(__name__)

ONLINE = 'online'
BUSY = 'busy'               # Pi is fine and running a command (possibly not sent by us)
NO_ARDUINO = 'no-arduino'
OFFLINE = 'offline'

# The Pi sends `?` with a 3 s limit, and reopening the serial port costs another 3 s
# (the Arduino reboots on open). Waiting less than that would report a healthy Pi as offline.
STATUS_TIMEOUT = 8
# Extra wait on top of the command timeout. The Pi must get the chance to report its
# own, specific timeout (TIMEOUT_NO_TERMINAL) before our HTTP request gives up.
COMMAND_TIMEOUT_BUFFER = 5


class PiError(Exception):
    """
    A call to the Pi failed.

    pi_status: what this failure says about the device (BUSY/NO_ARDUINO/OFFLINE),
               or None when it says nothing new (e.g. the Pi rejected the request).
    http_status: status code the PC API should answer with.
    """

    def __init__(self, message, pi_status, http_status=502):
        super().__init__(message)
        self.pi_status = pi_status
        self.http_status = http_status


@dataclass
class Probe:
    """Result of one status check. `info` holds what the Pi reported about itself."""
    status: str
    info: dict = field(default_factory=dict)


# Fields of /api/status worth keeping for the UI and for timeout decisions.
INFO_FIELDS = ('app_version', 'command_timeout', 'max_command_timeout', 'rig_name', 'running', 'arduino_error')


def _url(device, endpoint):
    return f"http://{device['host']}:{device['port']}{endpoint}"


def _json(resp):
    """The Pi should always answer JSON, but a broken one must not crash the PC side."""
    try:
        body = resp.json()
        return body if isinstance(body, dict) else {'raw': body}
    except ValueError:
        return {'raw': resp.text[:200]}


def _request(method, device, endpoint, timeout, **kwargs):
    try:
        resp = requests.request(method, _url(device, endpoint), timeout=timeout, **kwargs)
    except requests.exceptions.Timeout:
        raise PiError('Pi did not answer in time', OFFLINE, 504)
    except requests.exceptions.RequestException as e:
        raise PiError(f'Cannot reach Pi: {e.__class__.__name__}', OFFLINE, 502)

    if resp.status_code == 200:
        return _json(resp)

    body = _json(resp)
    error = body.get('error') or f'Pi returned HTTP {resp.status_code}'
    if resp.status_code == 409:
        running = (body.get('running') or {}).get('command')
        raise PiError(f'Busy running {running}' if running else 'Busy', BUSY, 409)
    if resp.status_code == 503:
        # Pi is fine, serial/Arduino is not (disconnected, timed out, desynced)
        raise PiError(error, NO_ARDUINO, 502)
    # 4xx/other 5xx: the Pi answered, so it is reachable. Don't guess a new status.
    raise PiError(error, None, 502)


def probe(device):
    """Status of one device plus what it reported about itself. Never raises."""
    try:
        body = _request('GET', device, '/api/status', STATUS_TIMEOUT)
    except PiError as e:
        return Probe(e.pi_status or OFFLINE)
    status = {'connected': ONLINE, 'busy': BUSY}.get(body.get('arduino_status'), NO_ARDUINO)
    return Probe(status, {k: body.get(k) for k in INFO_FIELDS})


def ping(device):
    """Ask the Arduino to answer once. Raises PiError on failure."""
    return _request('GET', device, '/api/ping', STATUS_TIMEOUT)


def commands(device):
    """The Pi's firmware command catalog. Raises PiError on failure."""
    return _request('GET', device, '/api/commands', STATUS_TIMEOUT)


def send_command(device, command, timeout):
    """Send one firmware command and wait for DONE/ERR. Raises PiError on failure."""
    return _request('POST', device, '/api/command', timeout + COMMAND_TIMEOUT_BUFFER,
                    json={'command': command, 'timeout': timeout})
