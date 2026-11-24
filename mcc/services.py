"""
The long-lived objects every route works with. create_app() fills them in once;
routes read them as `services.store`, `services.monitor`, ... (tests swap them out).
"""

import os

from .alerts import AlertEngine
from .beszel import BeszelClient, BeszelSync
from .devices import DeviceStore
from .monitor import StatusMonitor
from .room import Room, RoomConfig, RoomState
from .setups import SetupStore

# The axis table that ships with the code; data/room.json (settings.room_file) overrides it.
BUNDLED_ROOM = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'room.json')
# Example setups that ship with the code (read only), e.g. the 14 MS Teams V5 setups.
SETUP_LIBRARY = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'setup_library')

settings = None     # config.Settings
store = None        # devices.DeviceStore   — the device list (data/pi_config.json)
monitor = None      # monitor.StatusMonitor — status polling + one-command-per-device lock
health = None       # beszel.BeszelSync     — health data + two-way sync with Beszel
alerts = None       # alerts.AlertEngine    — alert rules and active alerts
room = None         # room.Room             — axis table, homed state, setup jobs
setups = None       # setups.SetupStore     — saved setups (data/setups/)


def init(app_settings):
    global settings, store, monitor, health, alerts, room, setups
    settings = app_settings
    # Start empty: devices come from the web UI or from the deploy tool (PUT /api/pi/<id>).
    store = DeviceStore(settings.config_file, {})
    monitor = StatusMonitor(store)
    alerts = AlertEngine(settings.alerts_file)
    health = BeszelSync(BeszelClient(settings.beszel_url, settings.beszel_email, settings.beszel_password), store)
    health.on_sync = alerts.evaluate      # alerts are re-evaluated after every health sync
    room = Room(RoomConfig([settings.room_file, BUNDLED_ROOM]), RoomState(settings.room_state_file))
    setups = SetupStore(settings.setups_dir, SETUP_LIBRARY)
