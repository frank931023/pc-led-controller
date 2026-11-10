"""
Background status polling + per-device command lock.

Why a background thread instead of probing inside each request:
  * GET /api/pis answers instantly from memory, so the page never waits on a slow Pi.
  * Many open browser tabs no longer multiply the probes sent to every Arduino.

Why the monitor also owns the command lock:
  The Pi answers /api/status by sending `?` over the same serial port, so while a
  40 s calibrate is running the probe waits on the Pi's serial lock, times out, and
  the device would flip to "offline" mid-command. Busy devices are therefore skipped.
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pi_client

log = logging.getLogger(__name__)

CHECKING = 'checking'   # added, never probed yet


class StatusMonitor:
    def __init__(self, store, interval=5.0, idle_after=30.0):
        self._store = store
        self._interval = interval
        self._idle_after = idle_after   # stop polling when nobody has looked for this long

        self._lock = threading.Lock()
        self._status = {}       # device_id -> status string
        self._updated = {}      # device_id -> monotonic time of last status change
        self._running = {}      # device_id -> command currently in flight
        self._last_seen = 0.0
        self._thread = None

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

    def set(self, device_id, status):
        if status is None:
            return
        with self._lock:
            self._status[device_id] = status
            self._updated[device_id] = time.monotonic()

    def forget(self, device_id):
        with self._lock:
            for d in (self._status, self._updated, self._running):
                d.pop(device_id, None)

    def check_soon(self, device_id):
        """Probe one device in the background (e.g. right after it was added)."""
        threading.Thread(target=self._probe_one, args=(device_id, time.monotonic()), daemon=True).start()

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
        ids = list(self._store.all().keys())
        ids = [i for i in ids if self.running(i) is None]
        if not ids:
            return
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=min(len(ids), 16)) as pool:
            for device_id in ids:
                pool.submit(self._probe_one, device_id, started)

    def _probe_one(self, device_id, started):
        device = self._store.get(device_id)
        if device is None:
            return
        status = pi_client.probe(device)
        with self._lock:
            # Drop stale results: a command started, or a fresher status was recorded
            # (command result / manual test) while this probe was in flight.
            if device_id in self._running or self._updated.get(device_id, 0) > started:
                return
            if self._status.get(device_id) != status:
                log.info('%s: %s -> %s', device_id, self._status.get(device_id, CHECKING), status)
            self._status[device_id] = status
            self._updated[device_id] = time.monotonic()
