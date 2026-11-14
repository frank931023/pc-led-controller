"""Helpers shared by the routes: JSON responses, request parsing, input validation."""

import re

from flask import jsonify, request

from . import services

# The ID ends up in URLs and in the HTML, so keep it to safe characters.
ID_RE = re.compile(r'^[A-Za-z0-9_.-]{1,32}$')
HOST_RE = re.compile(r'^[A-Za-z0-9.-]{1,253}$')      # hostname or IPv4, no scheme/path
MAX_NAME = 64
BAD_ID = 'Device ID: 1-32 characters, letters, digits, "-", "_" or "." only'
BAD_JSON = ('Request body must be a JSON object', 400)


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


def device_view(device_id, device, device_health=None):
    """A device as the API returns it: stored fields + live status + health."""
    return {**device, **services.monitor.snapshot(device_id), 'health': device_health}


def validate_device_fields(data):
    """
    Returns (fields, error_message). Names are only included when present in the
    request, so an update never clears a name it was not asked to touch.
    """
    host = str(data.get('host', '')).strip()
    if not HOST_RE.match(host):
        return None, 'Host must be a hostname or IPv4 address (no http://, no port)'
    try:
        port = int(data.get('port', 5000))
    except (TypeError, ValueError):
        return None, f"Invalid port: {data.get('port')!r}"
    if not 1 <= port <= 65535:
        return None, f'Port out of range: {port}'
    fields = {'host': host, 'port': port}
    for key in ('name', 'alternative_name'):
        if key in data:
            value = str(data.get(key) or '').strip()
            if len(value) > MAX_NAME:
                return None, f'Names are limited to {MAX_NAME} characters'
            fields[key] = value
    return fields, None
