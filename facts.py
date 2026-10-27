"""Deterministic debrief facts. Each fact cites the event IDs it is based on.

These are facts about what happened in the simulation. Interpretation (what to do differently) is a separate,
clearly labelled layer added later by the model; it must cite these facts, not replace them.
"""
import scenario as S


def _offset(minute: int) -> int:
    return minute - S.START


def _rate_around(inc, minute: int) -> tuple[float, float]:
    before = inc.history.get(minute - 1, inc.history[S.START - 1])['error_rate']
    after_minute = min(minute + 1, inc.now - 1)
    after = inc.history.get(after_minute, inc.history[inc.now - 1])['error_rate']
    return before, after


def summarize(inc) -> dict:
    events = inc.events
    by_id = {e['id']: e for e in events}
    looks = [e for e in events if e['kind'] == 'look']
    changes = [e for e in events if e['kind'] == 'action' and e['name'] != 'resolve']
    statuses = [e for e in events if e['kind'] == 'comms' and e['name'] == 'status']
    done = [e for e in events if e['kind'] == 'system' and e['arg'] in by_id]
    facts = []
    unhelpful = []   # IDs of changes that made things worse or did nothing

    def add(tone: str, text: str, refs: list):
        facts.append({'id': f'F{len(facts) + 1}', 'tone': tone, 'text': text, 'events': [r for r in refs if r]})

    # Outcome
    mitigated = next((e for e in events if e['name'] == 'mitigated'), None)
    if mitigated:
        cause = next((e for e in reversed(done) if e['minute'] <= inc.mitigated_at), None)
        cause_action = by_id[cause['arg']] if cause else None
        text = (f'Checkout was healthy (under 1% errors for 3 minutes) from {S.clock(inc.mitigated_at)}, '
                f'{_offset(inc.mitigated_at)} minutes into your shift')
        text += f", after: {cause_action['label']}." if cause_action else '.'
        add('good', text, [cause_action and cause_action['id'], cause and cause['id'], mitigated['id']])
    else:
        add('gap', f"Checkout was still failing at {inc.current()['error_rate']}% when the shift ended.", [])
    add('info', f'About {round(inc.failed):,} checkouts failed during your shift ({inc.now - S.START} simulated minutes).', [])

    # Evidence before the first change
    first_change = changes[0] if changes else None
    if first_change:
        before = [e for e in looks if int(e['id'][1:]) < int(first_change['id'][1:])]
        if before:
            names = '; '.join(dict.fromkeys(e['label'] for e in before))
            add('info', f"Before your first change ({first_change['label']}, {first_change['clock']}) you checked: {names}.",
                [e['id'] for e in before] + [first_change['id']])
        else:
            add('gap', f"Your first change ({first_change['label']}, {first_change['clock']}) came before you looked at any "
                       'metrics, logs, deploys or flags.', [first_change['id']])
    elif inc.ended:
        add('gap', 'You made no changes to the system.', [])

    # Rollback and the migration
    rollbacks = [e for e in changes if e['name'] == 'rollback']
    if rollbacks:
        first = rollbacks[0]
        notes = [e for e in looks if e['name'] == 'release' and e['arg'] == S.NEW and int(e['id'][1:]) < int(first['id'][1:])]
        migration = S.MIGRATION[inc.variant]
        if notes:
            add('good', f"You read the {S.NEW} release notes before rolling back (migration {migration['name']}: "
                        f"{'reversible' if migration['reversible'] else 'irreversible'}).", [notes[0]['id'], first['id']])
        else:
            add('gap', f"You rolled back at {first['clock']} without opening the {S.NEW} release notes, which list "
                       f"migration {migration['name']} ({'reversible' if migration['reversible'] else 'irreversible'}).",
                [first['id']])
    broken = [m for m in range(S.START, inc.now) if inc.history[m]['mode'] == 'broken_schema']
    if broken:
        unhelpful.append(rollbacks[0]['id'] if rollbacks else 'rollback')
        failed = sum(S.TRAFFIC * inc.history[m]['error_rate'] / 100 for m in broken)
        finished = next((e for e in done if e['name'] == 'rollout_done' and by_id[e['arg']]['name'] == 'rollback'), None)
        add('gap', f'The rollback broke almost every checkout: errors were about 98% from {S.clock(broken[0])} to '
                   f'{S.clock(broken[-1])} ({len(broken)} minutes, about {round(failed):,} failed checkouts).',
            [rollbacks[0]['id'] if rollbacks else None, finished and finished['id']])

    # Changes that did not help
    for e in done:
        action = by_id[e['arg']]
        before, after = _rate_around(inc, e['minute'])
        if action['name'] in ('scale', 'restart', 'failover_payments') or (action['name'] == 'flag' and action['arg'] != 'tax_v2_quotes'):
            unhelpful.append(action['id'])
        if action['name'] == 'scale' and after > before + 2:
            add('gap', f'After scaling to 24 pods ({e["clock"]}) the error rate rose from {before}% to {after}%.', [action['id'], e['id']])
        elif action['name'] == 'restart':
            add('info', f'The rolling restart ({action["clock"]}) did not lower the error rate: {before}% before it finished, {after}% after.',
                [action['id'], e['id']])
        elif action['name'] == 'failover_payments':
            add('gap', f'Failing over payments ({action["clock"]}) did not change checkout errors ({before}% → {after}%) and '
                       'raised payment fees by 0.4%.', [action['id'], e['id']])
        elif action['name'] == 'flag' and action['arg'] != 'tax_v2_quotes':
            add('gap', f"Changing {action['arg']} ({action['clock']}) did not change the error rate ({before}% → {after}%); "
                       'that flag is unrelated to tax quotes.', [action['id'], e['id']])
        elif action['name'] == 'raise_timeout' and after < before:
            add('info', f"Raising the timeout ({action['clock']}) cut errors from {before}% to {after}%, "
                        f"but p99 latency rose to {inc.history[min(e['minute'] + 1, inc.now - 1)]['p99_ms']} ms.",
                [action['id'], e['id']])

    # The red herring
    payments_looks = [e for e in looks if e['arg'] in ('payments', 'payments-gateway')]
    if payments_looks:
        add('info', f'The PayRail latency warning started at {S.clock(S.PAYMENTS_WARNING)}, '
                    f'{S.DEPLOY - S.PAYMENTS_WARNING} minutes before the {S.NEW} deploy.', [payments_looks[0]['id']])

    # Communication
    if statuses:
        first = statuses[0]
        tone = 'good' if first['minute'] - S.ALERT <= 10 else 'info'
        add(tone, f"You posted the first status update at {first['clock']}, {first['minute'] - S.ALERT} minutes after the page"
                  f"{'' if len(statuses) == 1 else f', and {len(statuses) - 1} more later'}.", [s['id'] for s in statuses])
    elif inc.ended:
        add('gap', 'You did not post a status update.', [])
    page = next((e for e in changes if e['name'] == 'page_tax'), None)
    if page:
        reply = next((e for e in events if e['name'] == 'tax_reply'), None)
        add('info', f"You paged the tax-service team at {page['clock']}" + (f"; they replied at {reply['clock']}." if reply else '.'),
            [page['id'], reply and reply['id']])

    # How it ended
    resolved = next((e for e in events if e['name'] == 'resolve'), None)
    if resolved:
        rate = inc.history[resolved['minute'] - 1]['error_rate']
        if inc.healthy_streak >= S.HEALTHY_STREAK:
            add('good', f"You declared it resolved at {resolved['clock']}, after errors stayed under 1% for "
                        f'{inc.healthy_streak} minutes.', [resolved['id']])
        else:
            add('gap', f"You declared it resolved at {resolved['clock']} while the error rate was {rate}%.", [resolved['id']])
    elif inc.ended == 'time':
        add('info', f'The {S.SHIFT_LIMIT}-minute shift limit was reached at {S.clock(inc.now)}.', [])

    stats = {
        'variant': inc.variant,
        'minutes_to_mitigate': _offset(inc.mitigated_at) if inc.mitigated_at else None,
        'failed': round(inc.failed),
        'first_change': first_change and {'label': first_change['label'], 'clock': first_change['clock'],
                                          'minutes_in': _offset(first_change['minute'])},
        'looks_before_first_change': len([e for e in looks if not first_change or int(e['id'][1:]) < int(first_change['id'][1:])]),
        'unhelpful_changes': len(dict.fromkeys(unhelpful)),
        'first_status_minutes_after_page': statuses[0]['minute'] - S.ALERT if statuses else None,
        'ended': inc.ended,
    }
    return {'stats': stats, 'facts': facts}


def compare(first: dict, second: dict) -> list[dict]:
    """Side-by-side rows for the original run and the replay, from recorded stats only."""
    def minutes(value):
        return 'not mitigated' if value is None else f'{value} min'

    def change(value):
        return '—' if not value else f"{value['label']} ({value['minutes_in']} min in)"

    def status(value):
        return 'none' if value is None else f'{value} min after the page'

    a, b = first['stats'], second['stats']
    rows = [
        {'label': 'Time to healthy checkout', 'first': minutes(a['minutes_to_mitigate']), 'second': minutes(b['minutes_to_mitigate'])},
        {'label': 'Failed checkouts during the shift', 'first': f"{a['failed']:,}", 'second': f"{b['failed']:,}"},
        {'label': 'First change', 'first': change(a['first_change']), 'second': change(b['first_change'])},
        {'label': 'Things checked before the first change', 'first': str(a['looks_before_first_change']),
         'second': str(b['looks_before_first_change'])},
        {'label': 'Changes that made things worse or did nothing', 'first': str(a['unhelpful_changes']),
         'second': str(b['unhelpful_changes'])},
        {'label': 'First status update', 'first': status(a['first_status_minutes_after_page']),
         'second': status(b['first_status_minutes_after_page'])},
    ]
    return [dict(row, id=f'C{i}') for i, row in enumerate(rows, 1)]
