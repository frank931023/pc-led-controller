"""
Health alerts, evaluated here from the data the Beszel sync already fetches.

Rules live in data/alerts.json and are edited from the Monitoring page:
  agent_down               the Pi's monitoring agent stopped reporting
  cpu / mem / disk / temp  value >= threshold

A condition must hold on CONFIRM_SAMPLES syncs in a row (≈20 s) before it fires, so a
one-off CPU spike does not raise an alert. Firing and resolving are kept in a short
event log and, if a webhook URL is set, posted as {"text": "..."} — the format Slack
and Teams incoming webhooks both accept.
"""

import copy
import json
import logging
import threading
import time
from collections import deque

import requests

from .storage import write_json_atomic

log = logging.getLogger(__name__)

CONFIRM_SAMPLES = 2
EVENT_LOG_SIZE = 200
WEBHOOK_TIMEOUT = 5

# key -> (label, health field, unit). agent_down has no threshold.
METRICS = {
    'cpu': ('CPU', 'cpu', '%'),
    'mem': ('Memory', 'mem', '%'),
    'disk': ('Disk', 'disk', '%'),
    'temp': ('Temperature', 'temp', '°C'),
}
DEFAULT_RULES = {
    'agent_down': {'enabled': True},
    'cpu': {'enabled': True, 'threshold': 90},
    'mem': {'enabled': True, 'threshold': 90},
    'disk': {'enabled': True, 'threshold': 90},
    'temp': {'enabled': True, 'threshold': 80},
}


class AlertError(Exception):
    pass


class AlertEngine:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._settings = self._load()
        self._active = {}         # (device_id, rule) -> {device_id, rule, text, value, since}
        self._pending = {}        # (device_id, rule) -> consecutive samples seen
        self._events = deque(maxlen=EVENT_LOG_SIZE)
        self._webhook_error = None

    # ----- settings -----

    def _load(self):
        settings = {'rules': copy.deepcopy(DEFAULT_RULES), 'webhook_url': ''}
        try:
            with open(self.path, encoding='utf-8') as f:
                saved = json.load(f)
            settings.update(validate(saved))
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AlertError) as e:
            # Bad file: run with defaults but don't overwrite it until the user saves.
            log.error('Cannot read %s (%s); using default alert rules', self.path, e)
        return settings

    def settings(self):
        with self._lock:
            return copy.deepcopy(self._settings)

    def update(self, data):
        """Validate and save new rules / webhook URL. Raises AlertError on bad input."""
        new = validate(data)
        with self._lock:
            settings = {**copy.deepcopy(self._settings), **new}
            write_json_atomic(self.path, settings)
            self._settings = settings
            # Re-evaluate from scratch with the new thresholds on the next sync.
            self._pending.clear()
        return self.settings()

    # ----- evaluation (called after every Beszel sync) -----

    def evaluate(self, devices, health):
        """devices: id -> device dict, health: id -> health dict or None."""
        with self._lock:
            rules = self._settings['rules']
            firing = {}
            for device_id, h in health.items():
                if not h or device_id not in devices:
                    continue
                name = _label(device_id, devices[device_id])
                if rules['agent_down']['enabled'] and h.get('status') == 'down':
                    firing[(device_id, 'agent_down')] = (f'{name}: monitoring agent is down', None)
                    continue   # the other values are stale while the agent is down
                if h.get('status') != 'up':
                    continue
                for key, (title, field, unit) in METRICS.items():
                    rule = rules[key]
                    value = h.get(field)
                    if rule['enabled'] and value is not None and value >= rule['threshold']:
                        firing[(device_id, key)] = (
                            f'{name}: {title} {round(value)}{unit} (limit {rule["threshold"]:g}{unit})', value)

            now = time.time()
            notify = []
            for key, (text, value) in firing.items():
                if key in self._active:
                    self._active[key].update(text=text, value=value)
                    continue
                self._pending[key] = self._pending.get(key, 0) + 1
                if self._pending[key] >= CONFIRM_SAMPLES:
                    del self._pending[key]
                    alert = {'device_id': key[0], 'rule': key[1], 'text': text, 'value': value, 'since': now}
                    self._active[key] = alert
                    self._events.appendleft({'at': now, 'kind': 'fired', **_public(alert)})
                    notify.append(f'🔴 {text}')

            for key in list(self._pending):
                if key not in firing:
                    del self._pending[key]
            for key in list(self._active):
                if key in firing:
                    continue
                alert = self._active.pop(key)
                if key[0] not in devices:
                    continue     # device removed: drop quietly, nothing to resolve
                self._events.appendleft({'at': now, 'kind': 'resolved', **_public(alert)})
                notify.append(f'✅ Resolved: {alert["text"]}')

            url = self._settings['webhook_url']
        for text in notify:
            log.info('Alert: %s', text)
            if url:
                threading.Thread(target=self._post, args=(url, text), daemon=True).start()

    # ----- read side -----

    def active(self):
        with self._lock:
            return sorted((_public(a) for a in self._active.values()), key=lambda a: a['since'])

    def state(self):
        with self._lock:
            return {'settings': copy.deepcopy(self._settings),
                    'active': sorted((_public(a) for a in self._active.values()), key=lambda a: a['since']),
                    'events': list(self._events),
                    'webhook_error': self._webhook_error}

    # ----- webhook -----

    def _post(self, url, text):
        try:
            resp = requests.post(url, json={'text': text}, timeout=WEBHOOK_TIMEOUT)
            resp.raise_for_status()
            self._webhook_error = None
        except requests.exceptions.RequestException as e:
            self._webhook_error = f'{e.__class__.__name__}: {e}'[:200]
            log.warning('Alert webhook failed: %s', self._webhook_error)

    def send_test(self):
        url = self.settings()['webhook_url']
        if not url:
            raise AlertError('No webhook URL set')
        try:
            resp = requests.post(url, json={'text': '🔔 Test alert from Motion Control Center'},
                                 timeout=WEBHOOK_TIMEOUT)
            resp.raise_for_status()
        except requests.exceptions.RequestException as e:
            self._webhook_error = f'{e.__class__.__name__}: {e}'[:200]
            raise AlertError(f'Webhook failed: {self._webhook_error}') from e
        self._webhook_error = None


def validate(data):
    """Returns the cleaned {rules?, webhook_url?} part of `data`. Raises AlertError."""
    if not isinstance(data, dict):
        raise AlertError('Body must be a JSON object')
    out = {}
    if 'rules' in data:
        rules_in = data['rules']
        if not isinstance(rules_in, dict):
            raise AlertError('rules must be an object')
        rules = copy.deepcopy(DEFAULT_RULES)
        for key, default in DEFAULT_RULES.items():
            given = rules_in.get(key, {})
            if not isinstance(given, dict):
                raise AlertError(f'rules.{key} must be an object')
            rules[key]['enabled'] = bool(given.get('enabled', default['enabled']))
            if 'threshold' in default:
                try:
                    threshold = float(given.get('threshold', default['threshold']))
                except (TypeError, ValueError):
                    raise AlertError(f'rules.{key}.threshold must be a number')
                limit = 150 if key == 'temp' else 100
                if not 1 <= threshold <= limit:
                    raise AlertError(f'rules.{key}.threshold must be between 1 and {limit}')
                rules[key]['threshold'] = threshold
        out['rules'] = rules
    if 'webhook_url' in data:
        url = str(data.get('webhook_url') or '').strip()
        if url and not url.startswith(('http://', 'https://')):
            raise AlertError('webhook_url must start with http:// or https://')
        if len(url) > 2000:
            raise AlertError('webhook_url is too long')
        out['webhook_url'] = url
    return out


def _label(device_id, device):
    return device.get('alternative_name') or device.get('name') or device_id


def _public(alert):
    return {k: alert[k] for k in ('device_id', 'rule', 'text', 'value', 'since')}

