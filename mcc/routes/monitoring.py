"""Pi health: Beszel hub state, linking devices to agents, history, containers, agent setup."""

import re

from flask import Blueprint, request

from .. import services
from ..beszel import HISTORY_RANGES, UNLINKED, BeszelError
from ..web import device_view, fail, json_object, ok

bp = Blueprint('monitoring', __name__)


def beszel_public_url():
    """Hub URL as seen from the browser's / the Pis' side of the network."""
    settings = services.settings
    if settings.beszel_public_url:
        return settings.beszel_public_url
    return f'{request.scheme}://{request.host.rsplit(":", 1)[0]}:{settings.beszel_port}'


def beszel_summary(devices):
    """Hub state + Beszel systems that belong to no device (for "Add as device" / "Link")."""
    health = services.health
    return {**health.state(),
            'unlinked': health.unlinked(devices) if health.client.configured else []}


def linked_system(pi_id):
    """(system_id, None) for a device linked to a known Beszel system, else (None, error response)."""
    device = services.store.get(pi_id)
    if device is None:
        return None, fail('Pi not found', 404)
    if not services.health.client.configured:
        return None, fail('Health monitoring is off (BESZEL_URL is empty)', 400)
    if services.health.system(device['beszel_system_id']) is None:
        return None, fail('Not linked to a monitoring agent', 404)
    return device['beszel_system_id'], None


@bp.get('/api/beszel')
def beszel_state():
    """Is health monitoring on, and can we reach the hub?"""
    return ok(beszel=beszel_summary(services.store.all()))


@bp.post('/api/pi/<pi_id>/beszel')
def link_beszel(pi_id):
    """
    Link a device to a Beszel system.
      {"system_id": "<id>"}  link to that system (taken away from any other device)
      {"system_id": null}    unlink and never auto-match again
      {"system_id": ""}      unlink and allow auto-matching by name again
    """
    store = services.store
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
    elif sid and services.health.system(sid) is None:
        return fail('Unknown Beszel system (it may have just been deleted)', 404)

    if sid not in ('', UNLINKED):
        # One system belongs to one device: release it from whoever had it.
        for other_id, other in store.all().items():
            if other_id != pi_id and other['beszel_system_id'] == sid:
                store.update(other_id, beszel_system_id=UNLINKED, beszel_name='')

    # beszel_name='' makes the next sync push this device's name to the system.
    device = store.update(pi_id, beszel_system_id=sid, beszel_name='')
    services.health.wake()
    return ok(pi_id=pi_id, pi=device_view(pi_id, device))


@bp.get('/api/pi/<pi_id>/health/history')
def health_history(pi_id):
    """CPU / memory / disk / temperature / load over time, e.g. ?range=24h (default 1h)."""
    if services.store.get(pi_id) is None:
        return fail('Pi not found', 404)
    range_key = request.args.get('range', '1h')
    if range_key not in HISTORY_RANGES:
        return fail(f'range must be one of: {", ".join(HISTORY_RANGES)}', 400)
    system_id, error = linked_system(pi_id)
    if error:
        return error
    try:
        history = services.health.client.history(system_id, range_key)
    except BeszelError as e:
        return fail(str(e), 502)
    return ok(pi_id=pi_id, range=range_key, ranges=list(HISTORY_RANGES), **history)


@bp.get('/api/pi/<pi_id>/containers')
def containers(pi_id):
    """Docker containers on that Pi with CPU / memory, from its monitoring agent."""
    system_id, error = linked_system(pi_id)
    if error:
        return error
    try:
        items = services.health.client.containers(system_id)
    except BeszelError as e:
        return fail(str(e), 502)
    return ok(pi_id=pi_id, containers=items)


@bp.get('/api/beszel/agent-env')
def beszel_agent_env():
    """
    The lines to paste into each Pi's .env so its agent registers with our hub.
    ⚠ Contains the registration token: anyone with it can add systems to the hub,
    so this route needs the API token when one is set (see app factory).
    """
    client = services.health.client
    if not client.configured:
        return fail('Beszel is not configured (BESZEL_URL is empty)', 400)
    try:
        agent = client.agent_settings()
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
        f'BESZEL_KEY="{agent["key"]}"',
        f'BESZEL_TOKEN={agent["token"]}',
    ])
    # env: ready to paste into a Pi's .env; key/token/hub_url: for deploy tools.
    return ok(env=env, hub_url=hub_url, key=agent['key'], token=agent['token'], warning=warning)
