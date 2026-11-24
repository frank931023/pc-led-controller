"""
The test scripts on the Scripts page. Each one plays ACQUA Automation against the real
motion API (in simulation, so no Pi is needed) and checks what ACQUA would rely on.

Scripts only use the axis table and the example setups, never fixed axis names, so they
keep working when room.json changes. Add one by writing a function with @script.
"""

from .runner import Script

SCRIPTS = []


def script(script_id, name, description, mode='auto', max_speed=None, interactive=False):
    def register(fn):
        SCRIPTS.append(Script(script_id, name, description, fn, mode, max_speed, interactive))
        return fn
    return register


# ════════════════════════════════════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════════════════════════════════════

def wired_axes(ctx):
    return [ax for ax in ctx.room.axes() if ax.wired]


def axis_targets(ctx, fraction):
    """Every wired axis at `fraction` of its travel, on the firmware's grid (mm / degree)."""
    return {ax.id: ax.to_room(ax.to_device(ax.min + (ax.max - ax.min) * fraction)) for ax in wired_axes(ctx)}


def moves(ctx, room_targets):
    """How many axes would really move (unknown positions count as moving)."""
    by_id = {ax.id: ax for ax in ctx.room.axes()}
    return sum(ctx.room.position(by_id[aid], simulated=True) != by_id[aid].to_device(v)
               for aid, v in room_targets.items())


def fresh_targets(ctx, min_moves=2):
    """Targets that move at least min_moves axes from where they are now."""
    targets = {}
    for fraction in (0.3, 0.7, 0.5):
        targets = axis_targets(ctx, fraction)
        if moves(ctx, targets) >= min_moves:
            break
    return targets


def send(ctx, label, room_targets):
    """POST a setup the way ACQUA does. Returns (job_id, job, ACQUA-format targets)."""
    targets = ctx.acqua.targets(ctx.room.axes(), room_targets)
    count = sum(len(c) for c in targets.values())
    ctx.step(f'POST /api/motion/jobs "{label}": {count} axes on {len(targets)} Pi(s)')
    answer = ctx.acqua.post(label, targets)
    ctx.require(answer.status == 202 and answer.body.get('job_id'),
                'accepted (HTTP 202 with a job_id)', f'HTTP {answer.status}: {answer.error}')
    return answer.body['job_id'], answer.body['job'], targets


def finish(ctx, job_id, job, timeout=None):
    """Poll until the job ends; the run fails if it does not end in time."""
    final = ctx.acqua.wait(job_id, timeout or job['eta_seconds'] * 2 + 30)
    ctx.require(final is not None, 'the job ended in time')
    return final


def check_done(ctx, final, targets):
    ctx.check(final['state'] == 'done', 'ACQUA sees state "done"', f'{final["state"]}: {final["error"]}')
    ctx.check(final.get('positions') == targets, 'reported positions = the targets sent',
              f'{final.get("positions")}')


# ════════════════════════════════════════════════════════════════════════════
#  Scripts
# ════════════════════════════════════════════════════════════════════════════

@script('one-setup', 'ACQUA sends one setup',
        'One setup change, end to end: POST the targets, poll every second like ACQUA, and '
        'check it ends "done" with every axis reported where it was asked.')
def one_setup(ctx):
    setups = ctx.library_setups()
    if setups:
        label, room_targets = setups[0]['name'], setups[0]['axes']
    else:
        label, room_targets = 'Spread', fresh_targets(ctx)
    job_id, job, targets = send(ctx, label, room_targets)
    check_done(ctx, finish(ctx, job_id, job), targets)


@script('library-sequence', 'Every example setup, one after another',
        'A batch run: each example setup (e.g. the 14 MS Teams V5 ones) is sent only after the '
        'previous one is done, as ACQUA does between test items.')
def library_sequence(ctx):
    setups = ctx.library_setups()
    ctx.require(setups, 'there are example setups to send')
    for number, setup in enumerate(setups, 1):
        ctx.log(f'Setup {number} of {len(setups)}')
        job_id, job, targets = send(ctx, setup['name'], setup['axes'])
        check_done(ctx, finish(ctx, job_id, job), targets)


@script('same-twice', 'The same setup twice',
        'The second time, every axis is already in place: the job must be "done" at once '
        'without moving anything.')
def same_twice(ctx):
    room_targets = fresh_targets(ctx)
    job_id, job, targets = send(ctx, 'First time', room_targets)
    check_done(ctx, finish(ctx, job_id, job), targets)

    job_id, job, targets = send(ctx, 'Second time', room_targets)
    ctx.check(job['state'] == 'done', 'done straight away', job['state'])
    ctx.check(job['progress']['total'] == 0, 'no axis moved', f'{job["progress"]["total"]} moves')
    ctx.check(job['progress']['unchanged'] == len(room_targets),
              f'all {len(room_targets)} axes counted as already in place', f'{job["progress"]["unchanged"]}')
    check_done(ctx, finish(ctx, job_id, job), targets)


@script('move-fails', 'A move fails halfway',
        'The firmware answers "Exceeded Max travel." on the second axis. ACQUA must get state '
        '"error" with a readable reason, the axes after it must not move, and the failed '
        'axis must not be reported as arrived.')
def move_fails(ctx):
    room_targets = fresh_targets(ctx, min_moves=2)
    ctx.room.sim.inject_fault(1, 'Exceeded Max travel.')
    ctx.log('Fault injected: the 2nd axis fails with "Exceeded Max travel."')
    job_id, job, targets = send(ctx, 'Fails on axis 2', room_targets)
    final = finish(ctx, job_id, job)

    ctx.check(final['state'] == 'error', 'ACQUA sees state "error"', final['state'])
    error = final['error'] or ''
    ctx.check('Exceeded Max travel' in error, 'the error says what the firmware said', error)
    steps = ctx.room.job(job_id)['steps']
    failed = steps[1]
    ctx.check(failed['device'] in error and failed['command'] in error,
              'the error names the Pi and the command', error)
    ctx.check(steps[0]['state'] == 'done' and failed['state'] == 'failed',
              'axis 1 arrived, axis 2 failed', f'{steps[0]["state"]}, {failed["state"]}')
    ctx.check(all(s['state'] == 'skipped' for s in steps[2:]), 'every axis after it was skipped')
    key = next(k for k in targets.get(failed['device'], {}) if failed['command'].startswith(k))
    ctx.check(key not in (final.get('positions') or {}).get(failed['device'], {}),
              'the failed axis is not reported as arrived')


@script('stop-midway', 'Stop a setup halfway',
        'Stop is pressed while the second axis moves. The axis moving then finishes (the '
        'firmware has no stop), every axis after it is skipped, ACQUA sees "aborted".',
        max_speed=5)
def stop_midway(ctx):
    job_id, job, _ = send(ctx, 'Stopped halfway', fresh_targets(ctx, min_moves=3))
    ctx.step('Waiting for the first axis to arrive')
    halfway = ctx.acqua.wait(job_id, job['eta_seconds'] * 2 + 30, until=lambda j: j['progress']['done'] >= 1)
    ctx.require(halfway and halfway['state'] == 'running', 'caught the job while it was moving',
                halfway and halfway['state'])

    ctx.step('POST /api/room/stop')
    ctx.check(ctx.acqua.stop().status == 200, 'stop accepted')
    final = finish(ctx, job_id, job)
    ctx.check(final['state'] == 'aborted', 'ACQUA sees state "aborted"', final['state'])
    ctx.check(bool(final['error']), 'with a reason ACQUA can record', repr(final['error']))
    states = [s['state'] for s in ctx.room.job(job_id)['steps']]
    ctx.check('pending' not in states and 'moving' not in states, 'nothing left half-done', ', '.join(states))
    ctx.check('skipped' in states, 'the axes not started were skipped', ', '.join(states))


@script('busy-409', 'A second setup while one is still moving',
        'Only one job at a time: a second POST while the first runs must be refused with 409 '
        '(ACQUA records it as "setup not applied"), and the first job is not disturbed.',
        mode='manual')
def busy_409(ctx):
    job_id, _, _ = send(ctx, 'First', fresh_targets(ctx))
    ctx.check(ctx.acqua.get(job_id).body['job']['state'] == 'running', 'the first job is running')

    ctx.step('POST a second setup at once')
    second = ctx.acqua.post('Second', ctx.acqua.targets(ctx.room.axes(), axis_targets(ctx, 0.9)))
    ctx.check(second.status == 409, 'refused with HTTP 409', f'HTTP {second.status}')
    ctx.check('still moving' in second.error, 'the reason says a setup is still moving', second.error)
    ctx.check(ctx.acqua.get(job_id).body['job']['state'] == 'running', 'the first job is still running')

    ctx.step('Stop the first job')
    ctx.acqua.stop()
    ctx.check(ctx.acqua.get(job_id).body['job']['state'] == 'aborted', 'the first job ended as "aborted"')


@script('bad-requests', 'Bad requests are refused',
        'Broken or impossible requests: each must be refused with a clear reason, and no job '
        'may start (nothing moves).')
def bad_requests(ctx):
    ax = next((a for a in wired_axes(ctx) if a.unit == 'm'), None) or wired_axes(ctx)[0]
    low, high = ax.device_travel()

    def one(value):
        return {'label': 'bad', 'targets': {ax.device: {ax.move_key: value}}}

    cases = [
        ('a Pi that does not exist', {'label': 'bad', 'targets': {'pi-999': {'SXM': 0}}}, None, 'no such device'),
        ('an unknown command', {'label': 'bad', 'targets': {ax.device: {'XYZ': 0}}}, None, 'unknown command'),
        (f'1 past the far end ({high + 1})', one(high + 1), None, 'outside its travel'),
        (f'1 before the near end ({low - 1})', one(low - 1), None, 'outside its travel'),
        ('a number written as text', one(str(low)), None, 'not a number'),
        ('true instead of a number', one(True), None, 'not a number'),
        ('no targets at all', {'label': 'bad', 'targets': {}}, None, 'non-empty'),
        ('a Pi with no commands', {'label': 'bad', 'targets': {ax.device: {}}}, None, 'no commands'),
        ('not JSON', None, 'this is not json', 'JSON object'),
    ]
    before = ctx.room.current()
    for name, body, raw, reason in cases:
        answer = ctx.acqua.send(body, raw)
        ctx.check(answer.status == 400 and reason in answer.error,
                  f'{name}: HTTP 400 "{reason}…"', f'HTTP {answer.status}: {answer.error}')
    after = ctx.room.current()
    ctx.check((before or {}).get('id') == (after or {}).get('id'), 'no job was started')


@script('travel-edges', 'Both ends of the travel',
        'The exact ends of an axis\'s travel are valid targets: 0 mm (the home switch) and the '
        'far end must both be accepted and reached.')
def travel_edges(ctx):
    ax = next((a for a in wired_axes(ctx) if a.unit == 'm'), None) or wired_axes(ctx)[0]
    low, high = ax.device_travel()
    for value in (low, high):
        job_id, job, targets = send(ctx, f'{ax.id} at {value}', {ax.id: ax.to_room(value)})
        check_done(ctx, finish(ctx, job_id, job), targets)


@script('wait-for-person', 'Wait for you: Mark as done',
        'A manual simulated job: the job window pops up and waits. Click "Mark as done" (or '
        '"Mark as failed") there; ACQUA gets exactly that answer.',
        mode='manual', interactive=True)
def wait_for_person(ctx):
    job_id, job, targets = send(ctx, 'Waiting for you', fresh_targets(ctx))
    ctx.log('The job window has popped up: click ✓ Mark as done (up to 5 minutes)')
    check_done(ctx, finish(ctx, job_id, job, timeout=300), targets)
