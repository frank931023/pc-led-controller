"""
Test scripts: try the whole ACQUA -> control center path without any Pi.

  runner.py   ScriptRunner: one script at a time in the background, its log, Stop;
              simulation is switched on for the run and restored afterwards
  acqua.py    AcquaClient: speaks the motion API exactly like ACQUA's HttpCrane
  catalog.py  the scripts (one function each, registered with @script)

The Scripts page in the sidebar lists them; GET/POST /api/scripts... (routes/scripts.py).
"""

from .catalog import SCRIPTS
from .runner import DEFAULT_SPEED, Script, ScriptError, ScriptRunner

__all__ = ['DEFAULT_SPEED', 'SCRIPTS', 'Script', 'ScriptError', 'ScriptRunner']
