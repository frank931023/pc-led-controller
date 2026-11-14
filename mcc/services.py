"""
The long-lived objects every route works with. create_app() fills them in once;
routes read them as `services.store`, `services.monitor`, ... (tests swap them out).
"""

from .alerts import AlertEngine
from .beszel import BeszelClient, BeszelSync
from .devices import DeviceStore
from .monitor import StatusMonitor

settings = None     # config.Settings
store = None        # devices.DeviceStore   — the device list (data/pi_config.json)
monitor = None      # monitor.StatusMonitor — status polling + one-command-per-device lock
health = None       # beszel.BeszelSync     — health data + two-way sync with Beszel
alerts = None       # alerts.AlertEngine    — alert rules and active alerts


def init(app_settings):
    global settings, store, monitor, health, alerts
    settings = app_settings
    # Start empty: devices come from the web UI or from the deploy tool (PUT /api/pi/<id>).
    store = DeviceStore(settings.config_file, {})
    monitor = StatusMonitor(store)
    alerts = AlertEngine(settings.alerts_file)
    health = BeszelSync(BeszelClient(settings.beszel_url, settings.beszel_email, settings.beszel_password), store)
    health.on_sync = alerts.evaluate      # alerts are re-evaluated after every health sync
