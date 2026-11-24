"""
A stand-in for ACQUA Automation: talks to the motion API exactly as ACQUA's HttpCrane
does (acqua/crane.py, docs/MCC_MOTION_API.md):

    POST /api/motion/jobs   {"label", "targets": {"pi-03": {"SZM": 3350, ...}}}
    GET  /api/motion/jobs/<id>  once per poll interval, until done / error / aborted

It goes through Flask's test client instead of the network on purpose: every request
takes the real path (routing, API token, JSON parsing, validation, status codes) with no
port, address or firewall to configure, so a script tests what ACQUA will really hit.
"""

import time
from dataclasses import dataclass

FINISHED = ('done', 'error', 'aborted')


@dataclass
class Answer:
    status: int
    body: dict

    @property
    def error(self):
        return self.body.get('error') or ''


class AcquaClient:
    def __init__(self, client, token, ctx):
        self._client = client
        self._headers = {'Authorization': f'Bearer {token}'} if token else {}
        self._ctx = ctx

    # ----- building requests -----

    @staticmethod
    def targets(axes, room_targets):
        """Room units by axis id -> ACQUA's format {device: {"SZM": firmware value}}.
        Axes not wired to a Pi cannot be addressed by ACQUA and are left out."""
        by_id = {a.id: a for a in axes}
        out = {}
        for aid, value in room_targets.items():
            ax = by_id.get(aid)
            if ax is not None and ax.wired:
                out.setdefault(ax.device, {})[ax.move_key] = ax.to_device(value)
        return out

    # ----- HTTP -----

    def post(self, label, targets, source='script'):
        return self.send({'label': label, 'targets': targets, 'source': source})

    def send(self, body=None, raw=None):
        """POST anything (raw: a string sent as is, for broken-JSON tests)."""
        if raw is not None:
            resp = self._client.post('/api/motion/jobs', data=raw, headers=self._headers,
                                     content_type='application/json')
        else:
            resp = self._client.post('/api/motion/jobs', json=body, headers=self._headers)
        return Answer(resp.status_code, resp.get_json(silent=True) or {})

    def get(self, job_id):
        resp = self._client.get(f'/api/motion/jobs/{job_id}', headers=self._headers)
        return Answer(resp.status_code, resp.get_json(silent=True) or {})

    def stop(self):
        resp = self._client.post('/api/room/stop', headers=self._headers)
        return Answer(resp.status_code, resp.get_json(silent=True) or {})

    # ----- polling -----

    def wait(self, job_id, timeout, until=None):
        """
        Poll like ACQUA until the job ends (or until(job) is true). Returns the last job
        view, or None after `timeout` seconds. Progress changes are written to the log.
        """
        deadline = time.monotonic() + timeout
        seen = None
        while True:
            answer = self.get(job_id)
            if answer.status != 200:
                self._ctx.log(f'GET /api/motion/jobs/{job_id} -> HTTP {answer.status}: {answer.error}')
                return None
            job = answer.body['job']
            progress = (job['state'], job['progress']['done'])
            if progress != seen:
                seen = progress
                self._ctx.log(f'  {job["state"]} · {job["progress"]["done"]}/{job["progress"]["total"]} axes')
            if job['state'] in FINISHED or (until and until(job)):
                return job
            if time.monotonic() >= deadline:
                return None
            self._ctx.sleep(self._ctx.poll_interval)
