"""
HTTP calls from the PC to a Pi's Flask service (see ../pi-arduino-connection-test-0717).

Pi API contract this module relies on:
  GET  /api/status   always 200 while Flask is up; arduino_status = connected | disconnected
  GET  /api/ping     200 if the Arduino answered, 503 otherwise
  POST /api/command  200 with arduino_result DONE|ERR, 400 bad request, 503 serial problem
"""

import logging

import requests

log = logging.getLogger(__name__)

ONLINE = 'online'
NO_ARDUINO = 'no-arduino'
OFFLINE = 'offline'

STATUS_TIMEOUT = 3          # seconds, for /api/status and /api/ping
COMMAND_TIMEOUT = 10        # default when the caller gives no timeout
# Extra wait on top of the command timeout. The Pi must get the chance to report its
# own, specific timeout (TIMEOUT_NO_TERMINAL) before our HTTP request gives up.
COMMAND_TIMEOUT_BUFFER = 5


class PiError(Exception):
    """
    A call to the Pi failed.

    pi_status: what this failure says about the device (ONLINE/NO_ARDUINO/OFFLINE),
               or None when it says nothing new (e.g. the Pi rejected the request).
    http_status: status code the PC API should answer with.
    """

    def __init__(self, message, pi_status, http_status=502):
        super().__init__(message)
        self.pi_status = pi_status
        self.http_status = http_status


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

    error = _json(resp).get('error') or f'Pi returned HTTP {resp.status_code}'
    if resp.status_code == 503:
        # Pi is fine, serial/Arduino is not (disconnected, timed out, desynced)
        raise PiError(error, NO_ARDUINO, 502)
    # 4xx/other 5xx: the Pi answered, so it is reachable. Don't guess a new status.
    raise PiError(error, None, 502)


def probe(device):
    """Return the device status. Never raises."""
    try:
        body = _request('GET', device, '/api/status', STATUS_TIMEOUT)
    except PiError as e:
        return e.pi_status or OFFLINE
    return ONLINE if body.get('arduino_status') == 'connected' else NO_ARDUINO


def ping(device):
    """Ask the Arduino to answer once. Raises PiError on failure."""
    return _request('GET', device, '/api/ping', STATUS_TIMEOUT)


def send_command(device, command, timeout=None):
    """Send one firmware command and wait for DONE/ERR. Raises PiError on failure."""
    payload = {'command': command}
    http_timeout = COMMAND_TIMEOUT + COMMAND_TIMEOUT_BUFFER
    if timeout is not None:
        payload['timeout'] = timeout
        http_timeout = timeout + COMMAND_TIMEOUT_BUFFER
    return _request('POST', device, '/api/command', http_timeout, json=payload)
