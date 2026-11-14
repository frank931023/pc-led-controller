"""The web page and the liveness check."""

from flask import Blueprint, render_template

from .. import services
from ..web import ok

bp = Blueprint('pages', __name__)


@bp.get('/')
def index():
    # No probing here: the page loads instantly and fetches /api/pis itself.
    return render_template('index.html')


@bp.get('/api/health')
def service_health():
    """Liveness check for Docker / deploy tools. Touches nothing slow."""
    return ok(auth_required=bool(services.settings.api_token))
