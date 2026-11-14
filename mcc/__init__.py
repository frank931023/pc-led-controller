"""
Motion Control Center (PC side) - controls X/Y/Z rails, rotation and tilt on several
Arduinos, each behind a Raspberry Pi running the Flask service in
../pi-arduino-connection-test-0717.

Package layout:
  config.py      settings from the environment (.env)
  services.py    the long-lived objects below, shared by all routes
  devices.py     device list persistence (data/pi_config.json)
  monitor.py     background status polling + one-command-per-device lock
  pi_client.py   HTTP calls to the Pis
  beszel.py      Pi health from the Beszel hub + two-way sync of the device list
  alerts.py      health alert rules, active alerts, webhook notifications
  storage.py     atomic JSON writes for the files in data/
  web.py         response / request / validation helpers for the routes
  routes/        HTTP routes, one Blueprint per area
"""

import hmac
import logging
import os
import time

from flask import Flask, request
from werkzeug.exceptions import HTTPException

from . import services
from .config import load_settings
from .devices import StoreError
from .routes import BLUEPRINTS
from .web import fail

log = logging.getLogger('mcc')

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def create_app(settings=None):
    services.init(settings or load_settings())
    app = Flask(__name__,
                template_folder=os.path.join(PROJECT_DIR, 'templates'),
                static_folder=os.path.join(PROJECT_DIR, 'static'))
    for bp in BLUEPRINTS:
        app.register_blueprint(bp)
    _install_hooks(app)
    return app


def _install_hooks(app):
    # Appended to static URLs so browsers fetch new JS/CSS after a restart/rebuild
    # instead of running a cached copy against a changed API.
    asset_version = str(int(time.time()))

    @app.context_processor
    def template_globals():
        return {'v': asset_version}

    @app.before_request
    def before_request():
        services.monitor.start()
        services.health.start()
        if request.path.startswith('/api/'):
            # Any API client (the page, a script, Ansible) keeps status polling alive.
            services.monitor.touch()
        token = services.settings.api_token
        if token and _needs_token() and not _has_token(token):
            return fail('Missing or wrong API token (Authorization: Bearer <API_TOKEN>)', 401)

    @app.errorhandler(HTTPException)
    def http_error(e):
        # API callers always get JSON, never Flask's HTML error page
        if request.path.startswith('/api/'):
            return fail(e.description, e.code)
        return e

    @app.errorhandler(StoreError)
    def store_error(e):
        return fail(f'Could not save the device list: {e}', 500)

    @app.errorhandler(Exception)
    def unexpected_error(e):
        log.exception('Unhandled error on %s %s', request.method, request.path)
        return fail('Internal server error (see server log)', 500)


def _needs_token():
    """Writes, plus the agent settings (they contain the Beszel registration token)."""
    if not request.path.startswith('/api/'):
        return False
    return request.method not in ('GET', 'HEAD', 'OPTIONS') or request.path == '/api/beszel/agent-env'


def _has_token(token):
    given = request.headers.get('Authorization', '').removeprefix('Bearer ').strip()
    return hmac.compare_digest(given.encode(), token.encode())
