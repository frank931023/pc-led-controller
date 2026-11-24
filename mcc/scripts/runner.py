"""
Runs one test script at a time in the background and keeps its log for the web page.

While a script runs, simulation is switched on in the mode the script needs; when it ends
(passed, failed, stopped or crashed) any job it left running is stopped and the
simulation settings are put back exactly as they were.
"""

import logging
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable

from .. import services
from ..room import ACTIVE, RoomError, Simulation, SimulationError
from .acqua import AcquaClient

log = logging.getLogger('mcc.scripts')

KEEP_RUNS = 10          # finished runs kept for the page (memory only)
LOG_LIMIT = 500         # lines per run; a runaway script cannot fill the memory
DEFAULT_SPEED = 10.0
SETTLE_SECONDS = 30     # how long to wait for a job stopped at the end of a run


@dataclass(frozen=True)
class Script:
    id: str
    name: str
    description: str
    run: Callable
    mode: str = 'auto'              # simulation mode it runs in: auto | manual
    max_speed: float | None = None  # e.g. a script that must catch a job halfway
    interactive: bool = False       # waits for someone to click in the job window

    def view(self):
        return {'id': self.id, 'name': self.name, 'description': self.description,
                'mode': self.mode, 'interactive': self.interactive}


class ScriptError(Exception):
    """A run could not be started. http_status is what the API answers with."""

    def __init__(self, message, http_status):
        super().__init__(message)
        self.http_status = http_status


class ScriptFailed(Exception):
    """Stops a script: a required condition did not hold."""


class ScriptStopped(Exception):
    """Stops a script: someone pressed Stop."""


class Context:
    """What a script works with: logging, checks, waiting, and an ACQUA client."""

    def __init__(self, run, room, app, sleep, poll_interval, stop):
        self._run = run
        self._sleep = sleep
        self._stop = stop
        self.room = room
        self.poll_interval = poll_interval
        self.acqua = AcquaClient(app.test_client(), services.settings.api_token, self)

    # ----- log -----

    def _line(self, level, text):
        lines = self._run['log']
        if len(lines) < LOG_LIMIT:
            lines.append({'at': round(time.time() - self._run['started'], 2), 'level': level, 'text': text})
        elif len(lines) == LOG_LIMIT:
            lines.append({'at': round(time.time() - self._run['started'], 2), 'level': 'warn',
                          'text': f'Log limit ({LOG_LIMIT} lines) reached: the rest is not shown'})

    def log(self, text):
        self._line('info', text)

    def warn(self, text):
        self._line('warn', text)

    def step(self, text):
        """A heading in the log: what the script does next."""
        self._line('step', text)

    def check(self, condition, text, detail=''):
        """Record a check. A failed check fails the run but the script goes on."""
        ok = bool(condition)
        self._run['checks']['passed' if ok else 'failed'] += 1
        self._line('pass' if ok else 'fail', text + (f' — {detail}' if detail and not ok else ''))
        return ok

    def require(self, condition, text, detail=''):
        """A check the rest of the script depends on: stops the script when it fails."""
        if not self.check(condition, text, detail):
            raise ScriptFailed(text)

    # ----- time -----

    def sleep(self, seconds):
        """Wait, but give up at once when the run is stopped."""
        if self._stop.wait(0) or self._stop_after(seconds):
            raise ScriptStopped()

    def _stop_after(self, seconds):
        deadline = time.monotonic() + seconds
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            self._sleep(min(left, 0.25))
            if self._stop.is_set():
                return True

    # ----- the library -----

    def library_setups(self):
        """The example setups that ship with the code (e.g. the 14 MS Teams V5 ones)."""
        return [s for s in services.setups.list() if s.get('readonly')]


class ScriptRunner:
    def __init__(self, scripts, sleep=time.sleep, poll_interval=1.0):
        self._scripts = OrderedDict((s.id, s) for s in scripts)
        self._sleep = sleep
        self._poll_interval = poll_interval
        self._lock = threading.Lock()
        self._runs = OrderedDict()          # id -> run (newest last)
        self._stop = threading.Event()

    def scripts(self):
        return [s.view() for s in self._scripts.values()]

    # ----- starting -----

    def start(self, script_id, app, speed=DEFAULT_SPEED):
        """Start a script in the background. Raises ScriptError (404 / 400 / 409)."""
        script = self._scripts.get(script_id)
        if script is None:
            raise ScriptError(f'No script "{script_id}"', 404)
        try:
            if speed is None:                       # check() reads None as "leave unchanged"
                raise SimulationError('"speed" must be a number')
            _, _, speed = Simulation.check(speed=speed)
        except SimulationError as e:
            raise ScriptError(str(e), 400) from e
        if script.max_speed:
            speed = min(speed, script.max_speed)
        with self._lock:
            running = self._running()
            if running:
                raise ScriptError(f'"{running["name"]}" is still running: wait or stop it first', 409)
            job = services.room.current()
            if job and job['state'] in ACTIVE:
                raise ScriptError(f'A setup is moving ("{job["name"]}"): wait or stop it first', 409)
            run = self._new_run(script, speed)
            self._stop.clear()
        context = Context(run, services.room, app, self._sleep, self._poll_interval, self._stop)
        threading.Thread(target=self._execute, args=(run, script, context),
                         daemon=True, name=f'script-{script.id}').start()
        return self._view(run)

    def _new_run(self, script, speed):
        run = {'id': uuid.uuid4().hex[:10], 'script': script.id, 'name': script.name,
               'mode': script.mode, 'speed': speed, 'state': 'running',
               'started': time.time(), 'finished': None,
               'checks': {'passed': 0, 'failed': 0}, 'log': []}
        self._runs[run['id']] = run
        while len(self._runs) > KEEP_RUNS:
            self._runs.popitem(last=False)
        return run

    def _running(self):
        return next((r for r in self._runs.values() if r['state'] == 'running'), None)

    # ----- running -----

    def _execute(self, run, script, ctx):
        room = services.room
        before = room.sim.settings()
        try:
            room.set_simulation(on=True, mode=script.mode, speed=run['speed'])
            ctx.log(f'Simulation on ({script.mode}, {run["speed"]:g}× speed): no Pi is moved')
            script.run(ctx)
            state = 'failed' if run['checks']['failed'] else 'passed'
        except ScriptStopped:
            ctx.warn('Stopped')
            state = 'stopped'
        except ScriptFailed:
            state = 'failed'
        except Exception as e:                       # a broken script must still end cleanly
            log.exception('Script %s crashed', script.id)
            ctx.check(False, f'The script crashed: {e.__class__.__name__}: {e}')
            state = 'error'
        finally:
            room.sim.take_fault()              # a fault the script injected but never used
            self._settle(room)
            try:
                room.set_simulation(**before)
            except RoomError as e:
                ctx.warn(f'Simulation settings not restored: {e}')
        with self._lock:
            run['state'], run['finished'] = state, time.time()

    def _settle(self, room):
        """Stop a job the script left running and wait until it has ended."""
        room.abort()
        deadline = time.monotonic() + SETTLE_SECONDS
        while time.monotonic() < deadline:
            job = room.current()
            if not job or job['state'] not in ACTIVE:
                return
            self._sleep(0.05)

    # ----- reading / stopping -----

    def _view(self, run):
        if run is None:
            return None
        return {**run, 'checks': dict(run['checks']), 'log': list(run['log'])}

    def get(self, run_id):
        with self._lock:
            return self._view(self._runs.get(run_id))

    def latest(self):
        with self._lock:
            return self._view(next(reversed(self._runs.values()), None))

    def stop(self):
        """Stop the running script (its job is stopped too). Returns the run, or None."""
        with self._lock:
            run = self._running()
            if run is None:
                return None
            self._stop.set()
            return self._view(run)
