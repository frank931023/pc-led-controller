"""Talking to one Pi: connection test, its command catalog, sending a command."""

from flask import Blueprint

from .. import pi_client, services
from ..dispatch import CommandError, resolve_timeout, run
from ..web import BAD_JSON, fail, json_object, ok

bp = Blueprint('commands', __name__)

MAX_COMMAND = 64


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
    return ok(pi_id=pi_id, **{k: catalog.get(k) for k in ('board', 'groups', 'home_sequence', 'default_timeout')})


@bp.post('/api/pi/<pi_id>/command')
def send_command(pi_id):
    """
    Send one firmware command, e.g. {"command": "SXM,500", "timeout": 40}.
    timeout (seconds) is optional; without it the Pi's own COMMAND_TIMEOUT is used.
    """
    if services.store.get(pi_id) is None:
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

    # A setup job owns its devices until it ends: a manual move in between would put an
    # axis somewhere the job does not expect.
    job = services.room.job_holding(pi_id)
    if job:
        return fail(f'A setup is moving this device ("{job}"); wait for it or stop it in the 3D room',
                    409, busy=True)

    try:
        body = run(pi_id, cmd, timeout)
    except CommandError as e:
        return fail(str(e), e.http_status, pi_status=services.monitor.status(pi_id), busy=e.busy)
    return ok(pi_id=pi_id, command=cmd, pi_response=body)
