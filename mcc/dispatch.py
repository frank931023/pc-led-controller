"""
Running one firmware command on one device.

Shared by the command API (buttons on the Devices page, scripts) and the room job runner
(setups), so every command goes through the same per-device lock, updates the device
status the same way, and reports to the room state, whatever sent it. That last part is
why a Home pressed by hand counts as "homed" when a setup runs later.
"""

import time

from . import pi_client, services


class CommandError(Exception):
    """A command could not be run. http_status is what the API answers with."""

    def __init__(self, message, http_status, pi_status=None, busy=False):
        super().__init__(message)
        self.http_status = http_status
        self.pi_status = pi_status
        self.busy = busy


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


def run(device_id, command, timeout, wait_busy=0.0):
    """
    Send one command and wait for the Pi's answer (DONE / ERR from the firmware).

    wait_busy: seconds to keep waiting for the device when another command of ours is
    still running on it, instead of failing right away. Raises CommandError.
    """
    monitor = services.monitor
    device = services.store.get(device_id)
    if device is None:
        raise CommandError(f'{device_id} is not in the device list', 404)

    # One command per device: the Arduino handles one blocking command at a time. The Pi
    # refuses a second one too (409), but checking here first saves the round trip.
    deadline = time.monotonic() + wait_busy
    while not monitor.try_begin(device_id, command):
        if time.monotonic() >= deadline:
            raise CommandError(f'Busy running {monitor.running(device_id)}', 409, busy=True)
        time.sleep(0.5)
    try:
        body = pi_client.send_command(device, command, timeout)
    except pi_client.PiError as e:
        monitor.set(device_id, e.pi_status)
        raise CommandError(str(e), e.http_status, pi_status=e.pi_status,
                           busy=e.pi_status == pi_client.BUSY) from e
    finally:
        monitor.end(device_id)

    monitor.set(device_id, pi_client.ONLINE)
    services.room.observe(device_id, command, body)
    return body
