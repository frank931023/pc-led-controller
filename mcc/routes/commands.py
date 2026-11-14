"""Talking to one Pi: connection test, its command catalog, sending a command."""

from flask import Blueprint

from .. import pi_client, services
from ..web import BAD_JSON, fail, json_object, ok

bp = Blueprint('commands', __name__)

MAX_COMMAND = 64


def resolve_timeout(device_id, raw):
    """
    Returns (timeout_seconds, error_message) for one command.

    Always resolves to a number: the Pi must be told explicitly, otherwise it would wait
    its own default (e.g. 130 s) while our HTTP request gave up much earlier, the device
    looked offline and our lock was released mid-command.
    """
    info = services.monitor.info(device_id)
    limit = info.get('max_command_timeout') or services.settings.max_command_timeout
    if raw in (None, ''):
        return info.get('command_timeout') or services.settings.default_command_timeout, None
    try:
        timeout = float(raw)
    except (TypeError, ValueError):
        return None, f'Invalid timeout value: {raw!r}'
    if not 1 <= timeout <= limit:
        return None, f'Timeout must be between 1 and {limit:g} seconds'
    return timeout, None


@bp.route('/api/pi/<pi_id>/test', methods=['GET', 'POST'])
def test_pi(pi_id):
    """Ask the Arduino to answer once. Response always carries pi_status."""
    monitor = services.monitor
    device = services.store.get(pi_id)
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


@bp.get('/api/pi/<pi_id>/commands')
def command_catalog(pi_id):
    """The Pi's firmware command catalog (buttons, timeouts, home sequence)."""
    device = services.store.get(pi_id)
    if device is None:
        return fail('Pi not found', 404)
    try:
        catalog = pi_client.commands(device)
    except pi_client.PiError as e:
        return fail(str(e), e.http_status)
    return ok(pi_id=pi_id, **{k: catalog.get(k) for k in ('groups', 'home_sequence', 'default_timeout')})


@bp.post('/api/pi/<pi_id>/command')
def send_command(pi_id):
    """
    Send one firmware command, e.g. {"command": "SXM,500", "timeout": 40}.
    timeout (seconds) is optional; without it the Pi's own COMMAND_TIMEOUT is used.
    """
    monitor = services.monitor
    device = services.store.get(pi_id)
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

    timeout, error = resolve_timeout(pi_id, data.get('timeout'))
    if error:
        return fail(error, 400)

    # One command per device: the Arduino handles one blocking command at a time. The Pi
    # refuses a second one too (409), but checking here first saves the round trip.
    if not monitor.try_begin(pi_id, cmd):
        return fail(f'Busy running {monitor.running(pi_id)}', 409, busy=True)
    try:
        body = pi_client.send_command(device, cmd, timeout)
    except pi_client.PiError as e:
        monitor.set(pi_id, e.pi_status)
        return fail(str(e), e.http_status, pi_status=monitor.status(pi_id),
                    busy=e.pi_status == pi_client.BUSY)
    finally:
        monitor.end(pi_id)

    monitor.set(pi_id, pi_client.ONLINE)
    return ok(pi_id=pi_id, command=cmd, pi_response=body)
