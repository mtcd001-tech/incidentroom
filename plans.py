"""Recovery plans: strict validation and deterministic execution against both scenario variants.

A plan arrives from the model as JSON, but nothing runs until `validate` accepts it. Running a plan is pure
simulation: the same plan always gives the same result. Both variants start fresh and are measured over the same
window, so a plan that stops early cannot look cheaper.
"""
import facts
import scenario as S
import sim

WINDOW = 20           # simulated minutes every plan is measured over, from the start of the shift
MAX_STEPS = 8         # executable steps on any path
MAX_NOTES = 5
CONDITIONS = {'migration_irreversible': f'the {S.NEW} release notes mark the database migration as irreversible'}
LOOKS = {
    'metrics': {name: f'{service} metrics' for name, service in sim.PANELS.items()},
    'logs': {name: f'{name} logs' for name in sim.LOG_SERVICES},
    'deploys': {None: 'deploy history'},
    'release': {r['version']: f"release notes for {r['version']}" for r in S.releases('A')},
    'flags': {None: 'feature flags'},
    'runbook': {None: 'the runbook'},
}
ACTS = [name for name in sim.ACTIONS]   # flag needs arg + to; status posts the standard update


class PlanError(ValueError):
    pass


def _arg(value):
    return None if value in (None, '') else value


def _name(value) -> str | None:
    """Model output is untrusted: only plain strings (or nothing) may be used as names."""
    return value if value is None or isinstance(value, str) else '<invalid>'


def _step(raw, where: str) -> dict:
    if not isinstance(raw, dict):
        raise PlanError(f'{where} is not a step.')
    if 'look' in raw:
        tool, arg = _name(raw['look']), _name(_arg(raw.get('arg')))
        if tool not in LOOKS or arg not in LOOKS[tool]:
            raise PlanError(f'{where} looks at something the simulator does not have ({tool} {arg or ""}).'.replace(' )', ')'))
        return {'look': tool, 'arg': arg}
    if 'act' in raw:
        action = _name(raw['act'])
        if action not in ACTS:
            raise PlanError(f'{where} uses an action the simulator does not have ({action}).')
        if action == 'flag':
            arg, to = _name(raw.get('arg')), _name(raw.get('to'))
            if arg not in S.FLAGS or to not in ('on', 'off'):
                raise PlanError(f'{where} changes a feature flag, but the flag name or on/off is missing.')
            return {'act': 'flag', 'arg': arg, 'to': to}
        return {'act': action, 'arg': None}
    if raw.get('wait') is True:
        return {'wait': True}
    raise PlanError(f'{where} is not a look, an action or a wait.')


def _notes(raw) -> list:
    if not isinstance(raw, list):
        return []
    return [' '.join(str(n).split())[:200] for n in raw if str(n).strip()][:MAX_NOTES]


def validate(raw) -> dict:
    """Return a clean plan or raise PlanError. Allows one top-level `if migration_irreversible` with then/else."""
    if not isinstance(raw, dict) or not isinstance(raw.get('steps'), list) or not raw['steps']:
        raise PlanError('The plan has no steps.')
    steps, branches = [], 0
    for i, item in enumerate(raw['steps'], 1):
        if isinstance(item, dict) and 'if' in item:
            branches += 1
            if branches > 1:
                raise PlanError('The plan has more than one decision; only one is supported.')
            if _name(item['if']) not in CONDITIONS:
                raise PlanError(f'Step {i} decides on something the simulator cannot check ({item["if"]}).')
            then, other = item.get('then'), item.get('else', [])
            if not isinstance(then, list) or not then or not isinstance(other, list):
                raise PlanError(f'Step {i} is a decision without steps to take.')
            steps.append({'if': item['if'], 'then': [_step(s, f'Step {i} (then)') for s in then],
                          'else': [_step(s, f'Step {i} (else)') for s in other]})
        else:
            steps.append(_step(item, f'Step {i}'))
    plain = sum(1 for s in steps if 'if' not in s)
    longest = plain + max((max(len(s['then']), len(s['else'])) for s in steps if 'if' in s), default=0)
    if longest > MAX_STEPS:
        raise PlanError(f'The plan has {longest} steps on one path; the limit is {MAX_STEPS}.')
    return {'steps': steps, 'assumptions': _notes(raw.get('assumptions')), 'unsupported': _notes(raw.get('unsupported'))}


STATUS_WORDS = ('status', 'update', 'post', 'announce', 'communicat', 'customer', 'notify')


def drop_unrequested_status(plan: dict, text: str) -> dict:
    """The model tends to turn "tell my manager" into a status update. Keep it only if the trainee asked for one."""
    if any(word in text.lower() for word in STATUS_WORDS):
        return plan
    keep = lambda steps: [s for s in steps if s.get('act') != 'status']
    steps = [dict(s, then=keep(s['then']), **{'else': keep(s['else'])}) if 'if' in s else s for s in plan['steps']]
    steps = [s for s in steps if s.get('act') != 'status' and not ('if' in s and not s['then'] and not s['else'])]
    if steps == plan['steps']:
        return plan
    if not steps:
        raise PlanError('The plan had no steps the simulator could run.')
    return dict(plan, steps=steps, assumptions=plan['assumptions'] + ['Left out a status update your plan did not ask for.'])


def describe(step: dict) -> str:
    if 'look' in step:
        return 'Look at ' + LOOKS[step['look']][step['arg']]
    if 'wait' in step:
        return f'Watch the dashboards for {sim.WAIT_MINUTES} minutes'
    if step['act'] == 'flag':
        return f"Turn {step['arg']} {step['to']}"
    if step['act'] == 'status':
        return 'Post a status update'
    return sim.ACTIONS[step['act']]['label']


def outline(plan: dict) -> list:
    """Numbered lines for the trainee to approve."""
    lines = []
    for n, step in enumerate(plan['steps'], 1):
        if 'if' in step:
            lines.append({'n': n, 'text': f"If {CONDITIONS[step['if']]}:", 'branch': None})
            lines += [{'n': f'{n}.{k}', 'text': describe(s), 'branch': 'then'} for k, s in enumerate(step['then'], 1)]
            if not step['then']:
                lines.append({'n': None, 'text': 'Do nothing more', 'branch': 'then'})
            lines.append({'n': None, 'text': 'Otherwise:', 'branch': None})
            lines += [{'n': f'{n}.{k}', 'text': describe(s), 'branch': 'else'} for k, s in enumerate(step['else'], 1)]
            if not step['else']:
                lines.append({'n': None, 'text': 'Do nothing more', 'branch': 'else'})
        else:
            lines.append({'n': n, 'text': describe(step), 'branch': None})
    return lines


def _run_step(inc, step: dict, seen: dict) -> str:
    """Run one step; return a note for the trainee ('' if it simply ran)."""
    if 'look' in step:
        result = inc.look(step['look'], step['arg'])
        if step['look'] == 'release' and step['arg'] == S.NEW:
            seen['migration'] = result['view']['migration']
        return ''
    if 'wait' in step:
        inc.wait()
        return ''
    if step['act'] == 'flag':
        want, now_on = step['to'] == 'on', inc.config['flags'][step['arg']]
        if f"flag:{step['arg']}" in inc.pending_kinds():
            if (not now_on) == want:
                return f"Skipped: {step['arg']} is already changing to {step['to']}."
            raise sim.CommandError(f"the earlier change of {step['arg']} to {'off' if want else 'on'} is still rolling out")
        if now_on == want:
            return f"Skipped: {step['arg']} is already {step['to']}."
        inc.act('flag', step['arg'])
        return ''
    inc.act(step['act'], None, '')
    return ''


def execute(plan: dict, variant: str) -> dict:
    """Run the plan on a fresh incident and measure it over the fixed window."""
    inc = sim.Incident(variant)
    trace, seen, stopped = [], {}, None
    end = S.START + WINDOW

    def run(steps, prefix):
        nonlocal stopped
        for k, step in enumerate(steps, 1):
            label = f'{prefix}{k}'
            if inc.now >= end:
                stopped = f'Step {label} was not reached within the {WINDOW}-minute window.'
                return
            if 'if' in step:
                migration = seen.get('migration', 'unseen')
                if migration == 'unseen':
                    stopped = (f'Step {label} decides on the migration, but the plan had not opened the {S.NEW} release '
                               'notes yet, so it stopped there.')
                    return
                irreversible = bool(migration) and not migration['reversible']
                branch = 'then' if irreversible else 'else'
                trace.append({'n': label, 'text': f"Decision: {'irreversible' if irreversible else 'not irreversible'} → "
                                                  f"{'first' if branch == 'then' else 'second'} branch", 'note': '', 'event': None})
                run(step[branch], f'{label}.')
                if stopped:
                    return
                continue
            before = len(inc.events)
            try:
                note = _run_step(inc, step, seen)
            except sim.CommandError as exc:
                stopped = f'Step {label} ({describe(step)}) could not run: {exc}'
                return
            event = inc.events[before]['id'] if len(inc.events) > before else None
            trace.append({'n': label, 'text': describe(step), 'note': note, 'event': event})

    run(plan['steps'], '')
    if inc.now < end:
        inc.run_until(end)
    summary = facts.summarize(inc)
    unsafe = sum(1 for m in range(S.START, inc.now) if inc.history[m]['mode'] == 'broken_schema')
    stats = dict(summary['stats'], final_error_rate=inc.current()['error_rate'], unsafe_minutes=unsafe,
                 stopped=stopped is not None, window=WINDOW)
    stats['verdict'] = verdict(stats)
    scoped = [dict(f, id=f"{variant}-{f['id']}") for f in summary['facts']]
    return {'variant': variant, 'trace': trace, 'stopped': stopped, 'stats': stats, 'facts': scoped, 'events': inc.events}


def verdict(stats: dict) -> str:
    if stats['unsafe_minutes']:
        return 'broke checkout'
    if stats['minutes_to_mitigate'] is None:
        return 'did not recover'
    return 'recovered and stayed healthy' if stats['healthy_at_end'] and not stats['relapsed'] else (
        'recovered, then relapsed' if not stats['healthy_at_end'] else 'recovered after a relapse')


def compare(first: dict, second: dict, labels=('A', 'B')) -> list[dict]:
    """Rows for two results, from recorded stats only (A vs B, or previous plan vs this plan)."""
    def healthy(s):
        return 'not reached' if s['minutes_to_mitigate'] is None else f"{s['minutes_to_mitigate']} min"
    rows = [
        ('Outcome', verdict(first), verdict(second)),
        ('Time to healthy checkout', healthy(first), healthy(second)),
        (f"Failed checkouts in {first['window']} min", f"{first['failed']:,}", f"{second['failed']:,}"),
        ('Error rate at the end', f"{first['final_error_rate']}%", f"{second['final_error_rate']}%"),
        ('Minutes with rollback breaking checkout', str(first['unsafe_minutes']), str(second['unsafe_minutes'])),
    ]
    return [{'id': f'C{i}', 'label': label, 'first': a, 'second': b, 'columns': list(labels)}
            for i, (label, a, b) in enumerate(rows, 1)]


def stress_test(plan: dict) -> dict:
    a, b = execute(plan, 'A'), execute(plan, 'B')
    return {'A': a, 'B': b, 'comparison': compare(a['stats'], b['stats'])}
