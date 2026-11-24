"""
The room: every movable axis, where each one is, and setup jobs that move them.

A setup is "every object in the room at a given position": {axis id: target} in room
units (metres / degrees), e.g. {"hats.mrp": 1.20, "hats.rot": 90}. ACQUA Automation and
the 3D room both send setups here; this package decides how to get there:

  axes.py     RoomConfig: the axis table (mcc/room.json, or data/room.json when it exists):
              which Pi and firmware axis each axis id is, its travel, the move order
  state.py    RoomState: per device, the Arduino's boot session, homed axes, last position
              sent (data/room_state.json). Simulation: the switch and pretend positions
  jobs.py     Room: checks a setup, then runs it as a background job, one axis at a time
  runners.py  how one axis is moved: on the real rig, or simulated

Why homing is tracked
---------------------
The firmware counts steps from wherever it was at power-on, so an absolute move is only
right after that axis has been homed (driven to its home switch) since the Arduino last
restarted. Opening the serial port resets a Mega, so a Pi reboot, a redeploy or a USB
reconnect all make the Arduino forget every position, silently. The Pi reports an
`arduino_session` that changes on every restart; when it changes, every axis on that
device counts as not homed, and the next setup homes it before moving it.

Positions are what we last *sent*, not measured: the steppers are open loop, and asking
the firmware (SXP...) would just echo its own step count back.

Simulation (no Pis yet)
-----------------------
With simulation on, a job sends nothing to the Pis. In "auto" mode it plays out by itself
(axis by axis, faster than real if asked); in "manual" mode it waits until someone on the
web page marks it done or failed. ACQUA polls the same job either way and cannot tell the
difference, so the whole ACQUA -> MCC path can be tried without hardware.
"""

from .axes import Axis, RoomConfig, RoomConfigError, firmware_failure, parse_axes
from .jobs import ACTIVE, Room, RoomError
from .runners import StepFailed
from .state import RoomState, Simulation, SimulationError

__all__ = ['ACTIVE', 'Axis', 'Room', 'RoomConfig', 'RoomConfigError', 'RoomError', 'RoomState',
           'Simulation', 'SimulationError', 'StepFailed', 'firmware_failure', 'parse_axes']
