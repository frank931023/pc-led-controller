"""Device list: list / get / add / create-or-update / edit / remove."""

from flask import Blueprint, request

from .. import services
from ..beszel import UNLINKED, BeszelError
from ..devices import DeviceExists, clean_park
from ..web import (BAD_ID, BAD_JSON, ID_RE, MAX_NAME, device_view, fail, json_object, ok,
                   validate_device_fields)
from .monitoring import beszel_summary

bp = Blueprint('devices', __name__)


@bp.get('/api/pis')
def list_pis():
    """All devices with their last known status (from memory, never blocks)."""
    devices = services.store.all()
    health = services.health.health_by_device(devices)
    return ok(pis={i: device_view(i, d, health.get(i)) for i, d in devices.items()},
              beszel=beszel_summary(devices), alerts=services.alerts.active())


@bp.get('/api/pi/<pi_id>')
def get_pi(pi_id):
    """One device, probed right now (blocks up to pi_client.STATUS_TIMEOUT)."""
    device = services.store.get(pi_id)
    if device is None:
        return fail('Pi not found', 404)
    services.monitor.probe_now(pi_id, device)
    return ok(pi=device_view(pi_id, device))


@bp.post('/api/pi')
def add_pi():
    data = json_object()
    if data is None:
        return fail(*BAD_JSON)

    pi_id = str(data.get('pi_id', '')).strip()
    if not ID_RE.match(pi_id):
        return fail(BAD_ID, 400)

    fields, error = validate_device_fields(data)
    if error:
        return fail(error, 400)

    # Optional: link straight to a Beszel system ("Add as device" from an unlinked agent).
    sid = str(data.get('beszel_system_id') or '').strip()
    if sid:
        if services.health.system(sid) is None:
            return fail('Unknown Beszel system (it may have just been deleted)', 400)
        if any(d['beszel_system_id'] == sid for d in services.store.all().values()):
            return fail('That Beszel system is already linked to another device', 400)
        fields['beszel_system_id'] = sid

    try:
        device = services.store.add(pi_id, fields)
    except DeviceExists:
        return fail(f"Pi '{pi_id}' already exists", 400)

    # Don't make the user wait for the probe; the UI shows "Checking" until it lands.
    services.monitor.check_soon(pi_id)
    services.health.wake()
    return ok(message=f"Pi '{pi_id}' added", pi_id=pi_id, pi=device_view(pi_id, device))


@bp.put('/api/pi/<pi_id>')
def upsert_pi(pi_id):
    """
    Create or update a device from a deploy tool's inventory (e.g. Ansible):
      {"host": "192.168.1.100", "port": 5000, "name": "pi-01"}
    Safe to repeat. alternative_name, the parking position and the Beszel link set in
    the UI are kept unless the body names them. 201 when created, 200 when it existed.
    """
    if not ID_RE.match(pi_id):
        return fail(BAD_ID, 400)
    data = json_object()
    if data is None:
        return fail(*BAD_JSON)
    fields, error = validate_device_fields(data)
    if error:
        return fail(error, 400)

    device, created = services.store.upsert(pi_id, fields)
    services.monitor.check_soon(pi_id)
    services.health.wake()
    body = ok(pi_id=pi_id, created=created, pi=device_view(pi_id, device))
    return (body, 201) if created else body


@bp.patch('/api/pi/<pi_id>')
def update_pi(pi_id):
    """
    Change what the UI edits in place:
      {"alternative_name": "Left rail"}       "" clears it (the UI falls back to name)
      {"park": {"SXM": 200, "RM": 90}}        parking position; {} clears it
    """
    data = json_object()
    if data is None:
        return fail(*BAD_JSON)
    changes = {}

    if 'alternative_name' in data:
        alt = str(data.get('alternative_name') or '').strip()
        if len(alt) > MAX_NAME:
            return fail(f'alternative_name is limited to {MAX_NAME} characters', 400)
        changes['alternative_name'] = alt

    if 'park' in data:
        park = data['park']
        if not isinstance(park, dict):
            return fail('park must be an object like {"SXM": 200}', 400)
        cleaned = clean_park(park)
        dropped = [k for k, v in park.items() if v is not None and k not in cleaned]
        if dropped:
            # Say so instead of silently saving less than the user typed.
            return fail(f'Invalid parking value for: {", ".join(map(str, dropped))}', 400)
        changes['park'] = cleaned

    if not changes:
        return fail('Nothing to change: send alternative_name and/or park', 400)

    device = services.store.update(pi_id, **changes)
    if device is None:
        return fail('Pi not found', 404)
    if 'alternative_name' in changes:
        services.health.wake()   # push the new name to the linked Beszel system right away
    return ok(pi_id=pi_id, pi=device_view(pi_id, device))


@bp.delete('/api/pi/<pi_id>')
def delete_pi(pi_id):
    """
    Remove a device. ?beszel=1 also deletes its linked Beszel system (and its history).
    ⚠ If that Pi's agent is still running it registers again a few seconds later and
    shows up under "Unlinked agents"; stop the agent on the Pi to remove it for good.
    """
    if services.monitor.running(pi_id):
        return fail('A command is still running on this device', 409)
    device = services.store.get(pi_id)
    if device is None:
        return fail('Pi not found', 404)

    beszel_deleted = False
    sid = device['beszel_system_id']
    if request.args.get('beszel') == '1' and sid not in ('', UNLINKED):
        # Beszel first: if it fails, nothing is deleted and the user can retry or untick.
        try:
            services.health.client.delete_system(sid)
            beszel_deleted = True
        except BeszelError as e:
            return fail(f'Device not removed: could not delete it from Beszel ({e})', 502)

    services.store.remove(pi_id)
    services.monitor.forget(pi_id)
    services.health.wake()
    return ok(message=f"Pi '{pi_id}' deleted", beszel_deleted=beszel_deleted)
