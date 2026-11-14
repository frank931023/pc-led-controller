"""HTTP routes, one Blueprint per area."""

from . import alerts, commands, devices, monitoring, pages

BLUEPRINTS = (pages.bp, devices.bp, commands.bp, monitoring.bp, alerts.bp)
