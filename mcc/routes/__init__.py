"""HTTP routes, one Blueprint per area."""

from . import alerts, commands, devices, monitoring, motion, pages, room, scripts

BLUEPRINTS = (pages.bp, devices.bp, commands.bp, monitoring.bp, alerts.bp, room.bp, motion.bp, scripts.bp)
