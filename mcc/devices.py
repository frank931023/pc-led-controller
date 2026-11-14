"""
Device list persistence (data/pi_config.json).

Only configuration is stored here. Runtime state such as online/offline lives in
monitor.StatusMonitor, so status polling never rewrites the file.
"""

import copy
import json
import logging
import math
import os
import re
import shutil
import threading
import time

from .storage import write_json_atomic

log = logging.getLogger(__name__)

DEFAULT_PORT = 5000


class StoreError(Exception):
    """The device list could not be written to disk."""


class DeviceExists(Exception):
    pass


class DeviceStore:
    """
    Thread-safe device list backed by a JSON file.

    The file is re-read whenever its modification time changes, so editing the JSON
    by hand while the server is running (e.g. setting alternative_name) still works.
    """

    def __init__(self, path, defaults):
        self.path = path
        self._lock = threading.RLock()
        self._devices = {}
        self._mtime = None

        with self._lock:
            if os.path.exists(path):
                self._reload_if_changed()
            else:
                self._devices = {k: _clean(k, v) for k, v in defaults.items()}
                self._write(self._devices)

    # ----- public API (all return copies, never internal references) -----

    def all(self):
        with self._lock:
            self._reload_if_changed()
            return copy.deepcopy(self._devices)

    def get(self, device_id):
        with self._lock:
            self._reload_if_changed()
            device = self._devices.get(device_id)
            return copy.deepcopy(device) if device else None

    def add(self, device_id, info):
        with self._lock:
            self._reload_if_changed()
            if device_id in self._devices:
                raise DeviceExists(device_id)
            new = copy.deepcopy(self._devices)
            new[device_id] = _clean(device_id, info)
            self._commit(new)
            return copy.deepcopy(new[device_id])

    def upsert(self, device_id, info):
        """
        Create the device, or overwrite the given fields of an existing one.
        Fields not in `info` (alternative_name set in the UI, the Beszel link) are kept,
        so a deploy tool can re-apply its inventory without undoing changes made here.
        Returns (device, created).
        """
        with self._lock:
            self._reload_if_changed()
            new = copy.deepcopy(self._devices)
            created = device_id not in new
            new[device_id] = _clean(device_id, {**new.get(device_id, {}), **info})
            if new != self._devices:
                self._commit(new)
            return copy.deepcopy(new[device_id]), created

    def update(self, device_id, **fields):
        """Returns the updated device, or None if it does not exist."""
        with self._lock:
            self._reload_if_changed()
            if device_id not in self._devices:
                return None
            new = copy.deepcopy(self._devices)
            new[device_id].update(fields)
            new[device_id] = _clean(device_id, new[device_id])
            self._commit(new)
            return copy.deepcopy(new[device_id])

    def remove(self, device_id):
        """Returns False if the device did not exist."""
        with self._lock:
            self._reload_if_changed()
            if device_id not in self._devices:
                return False
            new = copy.deepcopy(self._devices)
            del new[device_id]
            self._commit(new)
            return True

    # ----- internals -----

    def _commit(self, new):
        # Write first, swap in memory second: if the disk write fails the in-memory
        # list stays consistent with what is actually on disk.
        self._write(new)
        self._devices = new

    def _reload_if_changed(self):
        try:
            mtime = os.stat(self.path).st_mtime_ns
        except FileNotFoundError:
            return  # deleted while running: keep what we have, next save recreates it
        if mtime == self._mtime:
            return

        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                raw = json.load(f)
            pis = raw.get('pis') if isinstance(raw, dict) else None
            if not isinstance(pis, dict):
                raise ValueError("top-level 'pis' object is missing")
        except (OSError, ValueError) as e:
            # ⚠ Do NOT fall back to defaults here: the next save would overwrite the
            # user's real device list. Keep the broken file aside so nothing is lost,
            # and keep serving whatever we had in memory.
            backup = f"{self.path}.broken-{time.strftime('%Y%m%d-%H%M%S')}"
            try:
                shutil.copy2(self.path, backup)
            except OSError:
                backup = '(backup failed)'
            log.error("Cannot read %s (%s). Copy saved to %s; keeping previous device list.",
                      self.path, e, backup)
            self._mtime = mtime  # don't re-parse and re-backup on every request
            return

        self._devices = {str(k): _clean(str(k), v) for k, v in pis.items() if isinstance(v, dict)}
        self._mtime = mtime

    def _write(self, devices):
        try:
            write_json_atomic(self.path, {'pis': devices})
            self._mtime = os.stat(self.path).st_mtime_ns
        except OSError as e:
            log.error("Cannot save %s: %s", self.path, e)
            raise StoreError(str(e)) from e


def _clean(device_id, info):
    """Normalise one device entry; tolerates hand-edited JSON with missing or odd fields."""
    try:
        port = int(info.get('port', DEFAULT_PORT))
    except (TypeError, ValueError):
        port = DEFAULT_PORT
    return {
        'host': str(info.get('host', '')).strip(),
        'port': port,
        'name': str(info.get('name') or device_id).strip(),
        'alternative_name': str(info.get('alternative_name') or '').strip(),
        # Link to a Beszel system (see beszel_client.BeszelSync):
        #   ""      not linked yet, auto-match by name allowed
        #   "none"  user unlinked it on purpose, never auto-match
        #   other   the Beszel system record id
        'beszel_system_id': str(info.get('beszel_system_id') or '').strip(),
        # Name both sides agreed on at the last sync; tells which side renamed since.
        'beszel_name': str(info.get('beszel_name') or '').strip(),
        # Parking position, by move command prefix: {"SXM": 200, "RM": 90}. "Home all"
        # homes every axis, then moves each listed axis here. Axes left out stay home.
        'park': clean_park(info.get('park')),
    }


PARK_KEY_RE = re.compile(r'^[A-Z]{1,8}$')     # firmware move prefixes: SXM, SYM, SZM, RM, TM ...


def clean_park(park):
    """Keep only {PREFIX: finite number}; anything else (null, text, bad keys) is dropped."""
    if not isinstance(park, dict):
        return {}
    result = {}
    for key, value in park.items():
        if not PARK_KEY_RE.match(str(key)) or isinstance(value, bool):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            result[str(key)] = int(number) if number.is_integer() else number
    return result
