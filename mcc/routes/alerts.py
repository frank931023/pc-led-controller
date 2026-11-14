"""Health alert rules, active alerts and the webhook test."""

from flask import Blueprint

from .. import services
from ..alerts import AlertError
from ..web import BAD_JSON, fail, json_object, ok

bp = Blueprint('alerts', __name__)


@bp.get('/api/alerts')
def alert_state():
    """Rules, webhook URL, active alerts and the recent fired / resolved events."""
    return ok(**services.alerts.state())


@bp.put('/api/alerts')
def save_alerts():
    """{"rules": {"cpu": {"enabled": true, "threshold": 90}, ...}, "webhook_url": "https://..."}"""
    data = json_object()
    if data is None:
        return fail(*BAD_JSON)
    try:
        settings = services.alerts.update(data)
    except AlertError as e:
        return fail(str(e), 400)
    except OSError as e:
        return fail(f'Could not save the alert rules: {e}', 500)
    return ok(settings=settings)


@bp.post('/api/alerts/test')
def test_webhook():
    try:
        services.alerts.send_test()
    except AlertError as e:
        return fail(str(e), 502 if 'failed' in str(e) else 400)
    return ok(message='Test message sent')
