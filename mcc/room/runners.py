"""
How one step (one axis) of a job is carried out. The job loop in jobs.py is the same for
every kind of job; only the runner differs:

  RigRunner        the real rig: home first when needed, then the absolute move, through
                   the Pi (dispatch.run), with the firmware's answer checked
  SimulatedRunner  no hardware: the move plays out in time at `speed` times the nominal
                   speed, the pretend position is updated at the end

A runner raises StepFailed with a readable reason when the axis did not get there.
"""

from ..dispatch import CommandError, resolve_timeout, run
from .axes import firmware_failure

BUSY_WAIT = 30          # seconds a job waits for a manual command that is still running


class StepFailed(Exception):
    pass


class RigRunner:
    """Moves the real rig. See the package docstring for why homing is tracked."""

    def __init__(self, room):
        self.room = room

    def run_step(self, job, step):
        ax = step['_axis']
        state = self.room.state
        timeout, _ = resolve_timeout(ax.device, None)
        self.room.sync_sessions([ax.device])
        if ax.needs_home and not state.homed(ax.device, ax.ch):
            self._home(step, ax, timeout)
        session = state.session(ax.device)
        self._send(step, ax, step['command'], timeout, 'moving', step['to'])
        if ax.needs_home and state.session(ax.device) != session:
            # The Arduino restarted while moving: its zero is gone, so the move went
            # nowhere meaningful. Home and move again (once).
            self._home(step, ax, timeout)
            self._send(step, ax, step['command'], timeout, 'moving', step['to'])

    def _home(self, step, ax, timeout):
        step['homed_first'] = True
        self._send(step, ax, ax.home_command, timeout, 'homing', ax.to_room(0))

    def _send(self, step, ax, command, timeout, phase, goal):
        self.room.begin_phase(step, phase, goal)
        try:
            body = run(ax.device, command, timeout, wait_busy=BUSY_WAIT)
        except CommandError as e:
            raise StepFailed(f'{command}: {e}') from e
        self.room.end_phase(step, command, body.get('arduino_response') or [])
        failure = firmware_failure(command, body)
        if failure:
            raise StepFailed(failure)


class SimulatedRunner:
    """Plays a step out in time. Behaves like the rig where it matters: an axis whose
    position is not known is "homed" first, and a failed move leaves it unknown."""

    def __init__(self, room, sleep):
        self.room = room
        self._sleep = sleep

    def run_step(self, job, step):
        ax = step['_axis']
        sim = self.room.sim
        if ax.needs_home and not step['known_from']:
            step['homed_first'] = True
            self._play(step, 'homing', ax.to_room(0), job['speed'])
        fault = job['_fault']
        if fault and fault['step'] == job['index']:
            # stops halfway, as a real failure would: where it is now is unknown
            self._play(step, 'moving', (self.room.estimate(step) + step['to']) / 2, job['speed'])
            sim.forget(ax.id)
            raise StepFailed(f'{step["command"]}: {fault["error"]}')
        self._play(step, 'moving', step['to'], job['speed'])
        sim.arrive({ax.id: step['to']})

    def _play(self, step, phase, goal, speed):
        seconds = self.room.begin_phase(step, phase, goal, speed)
        self._sleep(seconds)
        self.room.end_phase(step, f'{phase} (simulated)', [])
