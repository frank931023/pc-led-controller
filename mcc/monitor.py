"""
Background status polling + per-device command lock.

Why a background thread instead of probing inside each request:
  * GET /api/pis answers instantly from memory, so the page never waits on a slow Pi.
  * Many open browser tabs no longer multiply the probes sent to every Arduino.

Why the monitor also owns the command lock:
  Our own probe would only be answered "busy" while our command runs, so busy devices
  are skipped. The lock also refuses a second command from this PC immediately (another
  tab, "Home all" overlapping a click). Commands sent to the Pi by anyone else (its local
  page, a script) are refused by the Pi itself with 409 and show up here as status "busy".
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import pi_client

log = logging.getLogger(__name__)

CHECKING = 'checking'   # added, never probed yet


class StatusMonitor:
    def __init__(self, store, interval=5.0, idle_after=30.0):
        self._store = store
        self._interval = interval
        self._idle_after = idle_after   # stop polling when no API client has asked for this long

        self._lock = threading.Lock()
        self._status = {}       # device_id -> status string
        self._updated = {}      # device_id -> monotonic time of last status change
        self._info = {}         # device_id -> what the Pi last reported (version, timeouts, running)
        self._running = {}      # device_id -> command this PC currently has in flight
        self._probing = set()   # devices with a probe in flight
        self._last_seen = 0.0
        self._thread = None
        # Probes run independently: an offline Pi waiting out its timeout must not
        # delay the next check of the healthy ones.
        self._pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix='probe')

    # ----- lifecycle -----

    def start(self):
        """Idempotent; safe to call from every request."""
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._loop, name='status-monitor', daemon=True)
                self._thread.start()

    def touch(self):
        """Mark that a client is watching; polling resumes immediately if it was idle."""
        self._last_seen = time.monotonic()

    # ----- status -----

    def status(self, device_id):
        with self._lock:
            return self._status.get(device_id, CHECKING)

    def info(self, device_id):
        with self._lock:
            return dict(self._info.get(device_id, {}))

    def snapshot(self, device_id):
        """Runtime fields merged into a device for the API."""
        with self._lock:
            info = self._info.get(device_id, {})
            remote = info.get('running') or {}
            updated = self._updated.get(device_id)
            return {
                'status': self._status.get(device_id, CHECKING),
                # Ours first; otherwise whatever the Pi says is running (e.g. its local page).
                'running': self._running.get(device_id) or remote.get('command'),
                'checked_ago': round(time.monotonic() - updated, 1) if updated else None,
                'app_version': info.get('app_version'),
                'rig_name': info.get('rig_name'),
                'command_timeout': info.get('command_timeout'),
                'max_command_timeout': info.get('max_command_timeout'),
            }

    def set(self, device_id, status):
        if status is None:
            return
        with self._lock:
            self._record(device_id, status)

    def forget(self, device_id):
        with self._lock:
            for d in (self._status, self._updated, self._info, self._running):
                d.pop(device_id, None)

    def check_soon(self, device_id):
        """Probe one device in the background (e.g. right after it was added)."""
        self._submit_probe(device_id, time.monotonic())

    def probe_now(self, device_id, device):
        """Probe right away and record the result (skipped while our command runs)."""
        if self.running(device_id) is None:
            self._apply(device_id, pi_client.probe(device), time.monotonic())

    # ----- command lock -----

    def try_begin(self, device_id, command):
        """Claim the device for one command. False if another command is running."""
        with self._lock:
            if device_id in self._running:
                return False
            self._running[device_id] = command
            return True

    def end(self, device_id):
        with self._lock:
            self._running.pop(device_id, None)

    def running(self, device_id):
        """Command this PC has in flight on the device, or None."""
        with self._lock:
            return self._running.get(device_id)

    # ----- polling -----

    def _loop(self):
        while True:
            if time.monotonic() - self._last_seen < self._idle_after:
                try:
                    self.poll_all()
                except Exception:   # never let the monitor thread die
                    log.exception('Status poll failed')
            time.sleep(self._interval)

    def poll_all(self):
        started = time.monotonic()
        for device_id in self._store.all():
            if self.running(device_id) is None:
                self._submit_probe(device_id, started)

    def _submit_probe(self, device_id, started):
        """Queue a probe unless one is already in flight for this device."""
        with self._lock:
            if device_id in self._probing:
                return
            self._probing.add(device_id)
        self._pool.submit(self._probe_one, device_id, started)

    def _probe_one(self, device_id, started):
        try:
            device = self._store.get(device_id)
            if device is not None:
                self._apply(device_id, pi_client.probe(device), started)
        except Exception:   # a bad device entry must not kill the pool worker silently
            log.exception('Probe of %s failed', device_id)
        finally:
            with self._lock:
                self._probing.discard(device_id)

    def _apply(self, device_id, probe, started):
        with self._lock:
            # Drop stale results: a command started, or a fresher status was recorded
            # (command result / manual test) while this probe was in flight.
            if device_id in self._running or self._updated.get(device_id, 0) > started:
                return
            if probe.info:
                self._info[device_id] = probe.info
            self._record(device_id, probe.status)

    def _record(self, device_id, status):
        """Caller holds self._lock."""
        if self._status.get(device_id) != status:
            log.info('%s: %s -> %s', device_id, self._status.get(device_id, CHECKING), status)
        self._status[device_id] = status
        self._updated[device_id] = time.monotonic()
        if status != pi_client.BUSY and device_id in self._info:
            # Whatever the Pi was running before has finished.
            self._info[device_id] = {**self._info[device_id], 'running': None}
