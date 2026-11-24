"""
Test scripts (no Pi needed), see mcc/scripts/.

  GET  /api/scripts              the scripts, and the latest run
  POST /api/scripts/<id>/run     start one: optional {"speed": 1 … 100} -> 202 {run}
  GET  /api/scripts/runs/<id>    a run with its log (poll it while state is "running")
  POST /api/scripts/stop         stop the running script (and its job)
"""

from flask import Blueprint, current_app

from .. import services
from ..scripts import DEFAULT_SPEED, ScriptError
from ..web import BAD_JSON, fail, json_object, ok

bp = Blueprint('scripts', __name__)


@bp.get('/api/scripts')
def list_scripts():
    return ok(scripts=services.scripts.scripts(), run=services.scripts.latest())


@bp.post('/api/scripts/<script_id>/run')
def run_script(script_id):
    data = json_object()
    if data is None:
        return fail(*BAD_JSON)
    try:
        run = services.scripts.start(script_id, current_app._get_current_object(),
                                     speed=data.get('speed', DEFAULT_SPEED))
    except ScriptError as e:
        return fail(str(e), e.http_status)
    return ok(run=run), 202


@bp.get('/api/scripts/runs/<run_id>')
def get_run(run_id):
    run = services.scripts.get(run_id)
    return ok(run=run) if run else fail('No such run (the last 10 are kept in memory)', 404)


@bp.post('/api/scripts/stop')
def stop_script():
    run = services.scripts.stop()
    return ok(run=run) if run else fail('No script is running', 409)
