"""Deterministic incident simulation. The model never decides what an action does; this module does.

The service state is a small config (release, flags, timeout, replicas, payments route). Each minute's metrics
come from that config, so the same commands always produce the same timeline.
"""
import math
import zlib

import scenario as S

LOOK_MINUTES = 1
ACTION_MINUTES = 1
WAIT_MINUTES = 2

PANELS = {'checkout': 'checkout-api', 'tax': 'tax-service', 'payments': 'payments-gateway', 'database': 'orders-db'}
LOG_SERVICES = ('checkout-api', 'tax-service', 'payments-gateway', 'orders-db')

ACTIONS = {
    'rollback': {'label': f'Roll back checkout-api to {S.OLD}', 'delay': 4},
    'roll_forward': {'label': f'Redeploy checkout-api {S.NEW}', 'delay': 4},
    'flag': {'label': 'Toggle a feature flag', 'delay': 1},
    'raise_timeout': {'label': 'Raise the tax client timeout to 3000 ms', 'delay': 2},
    'scale': {'label': 'Scale checkout-api from 12 to 24 pods', 'delay': 3},
    'restart': {'label': 'Rolling restart of checkout-api', 'delay': 2},
    'failover_payments': {'label': 'Fail over payments to the backup provider', 'delay': 2},
    'page_tax': {'label': 'Page the tax-service team', 'delay': 5},
    'status': {'label': 'Post a status update', 'delay': 0},
}


class CommandError(ValueError):
    pass


def snapshot(config: dict) -> dict:
    """An independent copy of the service config, so later changes never rewrite earlier minutes."""
    return dict(config, flags=dict(config['flags']))


def wobble(minute: int, key: int) -> float:
    """Deterministic noise in [-1, 1]."""
    return 0.6 * math.sin(minute * 1.31 + key * 2.17) + 0.4 * math.sin(minute * 0.47 + key)


def mode_for(config: dict) -> str:
    if config['version'] == S.OLD and config['schema_dropped']:
        return 'broken_schema'
    if config['version'] == S.NEW and config['flags']['tax_v2_quotes']:
        return 'tax_slow'
    return 'ok'


def metrics_for(minute: int, config: dict, restarting: bool = False) -> dict:
    mode = mode_for(config)
    m = {'minute': minute, 'mode': mode,
         'error_rate': 0.4 + 0.15 * wobble(minute, 1),
         'p99_ms': 420 + 30 * wobble(minute, 2),
         'tax_p99_ms': 180 + 15 * wobble(minute, 3),
         'tax_busy_pct': 35 + 5 * wobble(minute, 4),
         'pay_success_pct': 99.1 + 0.2 * wobble(minute, 5),
         'pay_p95_ms': (640 if minute >= S.PAYMENTS_WARNING else 310) + 40 * wobble(minute, 6),
         'db_cpu_pct': 38 + 3 * wobble(minute, 7),
         'db_errors': 0}
    if config['payments'] == 'backup':
        m['pay_p95_ms'] = 520 + 30 * wobble(minute, 6)
    if mode == 'tax_slow':
        doubled = config['replicas'] > 12   # more checkout pods send more concurrent quotes to a saturated service
        m['tax_p99_ms'] = (1700 if doubled else 1350) + 60 * wobble(minute, 3)
        m['tax_busy_pct'] = 100
        if config['tax_timeout_ms'] >= 3000:
            m['error_rate'] = (6.0 if doubled else 2.6) + 0.5 * wobble(minute, 1)
            m['p99_ms'] = 2950 + 80 * wobble(minute, 2)
        else:
            m['error_rate'] = (27.0 if doubled else 21.0) + 1.5 * wobble(minute, 1)
            m['p99_ms'] = 880 + 30 * wobble(minute, 2)
    elif mode == 'broken_schema':
        m['error_rate'] = 98.5 + 0.5 * wobble(minute, 1)
        m['p99_ms'] = 160 + 10 * wobble(minute, 2)
        m['db_errors'] = round(S.TRAFFIC * 0.98)
        m['db_cpu_pct'] = 30 + 2 * wobble(minute, 7)
    if restarting:
        m['error_rate'] = min(100.0, m['error_rate'] + 3.0)
    for key in ('error_rate', 'tax_busy_pct', 'pay_success_pct', 'db_cpu_pct'):
        m[key] = round(m[key], 1)
    for key in ('p99_ms', 'tax_p99_ms', 'pay_p95_ms'):
        m[key] = round(m[key])
    return m


class Incident:
    def __init__(self, variant: str = 'A'):
        if variant not in S.VARIANTS:
            raise CommandError('Unknown scenario variant.')
        self.variant = variant
        self.now = S.START
        self.config = {'version': S.NEW, 'flags': {name: True for name in S.FLAGS}, 'tax_timeout_ms': 800,
                       'replicas': 12, 'payments': 'primary', 'schema_dropped': variant == 'B'}
        self.pending = []          # dicts: due, kind, arg, event
        self.restart_until = None
        self.history = {}
        self.configs = {}          # minute -> config snapshot in effect that minute (logs are rendered from it)
        self.events = []
        self.failed = 0.0
        self.healthy_streak = 0
        self.mitigated_at = None   # first minute of the first healthy streak
        self.paged_tax = False
        self.ended = None          # None, 'resolved' or 'time'
        self._seed_history()

    # ---- timeline -------------------------------------------------------------------------------------------
    def _seed_history(self):
        for minute in range(S.HISTORY_START, S.START):
            before = minute < S.INCIDENT
            config = dict(self.config, version=S.OLD if before else S.NEW,
                          schema_dropped=self.variant == 'B' and minute >= S.INCIDENT)
            self.configs[minute] = snapshot(config)
            self.history[minute] = metrics_for(minute, config)

    def _event(self, kind: str, name: str, label: str, detail: str = '', arg=None, minute=None) -> dict:
        event = {'id': f'E{len(self.events) + 1}', 'minute': self.now if minute is None else minute,
                 'clock': S.clock(self.now if minute is None else minute), 'kind': kind, 'name': name,
                 'arg': arg, 'label': label, 'detail': detail}
        self.events.append(event)
        return event

    def _advance(self, minutes: int):
        for _ in range(minutes):
            if self.ended:
                return
            self._apply_due()
            restarting = self.restart_until is not None and self.now < self.restart_until
            m = metrics_for(self.now, self.config, restarting)
            self.configs[self.now] = snapshot(self.config)
            self.history[self.now] = m
            self.failed += S.TRAFFIC * m['error_rate'] / 100
            if m['error_rate'] <= S.HEALTHY:
                self.healthy_streak += 1
                if self.healthy_streak == S.HEALTHY_STREAK and self.mitigated_at is None:
                    self.mitigated_at = self.now - S.HEALTHY_STREAK + 1
                    self._event('system', 'mitigated', 'Checkout errors below 1% for 3 minutes',
                                f"Error rate {m['error_rate']}% at {S.clock(self.now)}")
            else:
                self.healthy_streak = 0
            self.now += 1
            if self.now >= S.START + S.SHIFT_LIMIT:
                self.ended = 'time'
                self._event('system', 'time', 'Shift limit reached',
                            'The incident commander took over after 40 minutes.')

    def _apply_due(self):
        for item in [p for p in self.pending if p['due'] <= self.now]:
            self.pending.remove(item)
            kind, arg, cause = item['kind'], item['arg'], item['event']['id']
            if kind == 'rollback':
                self.config['version'] = S.OLD
                detail = 'All 12 pods run v2.13.2.'
                if self.config['schema_dropped']:
                    detail += ' Orders are failing: column "tax_amount" does not exist.'
                self._event('system', 'rollout_done', f'Rollback to {S.OLD} finished', detail, arg=cause)
            elif kind == 'roll_forward':
                self.config['version'] = S.NEW
                self._event('system', 'rollout_done', f'Redeploy of {S.NEW} finished', 'All 12 pods run v2.14.0.', arg=cause)
            elif kind == 'flag':
                value = not self.config['flags'][arg]
                self.config['flags'][arg] = value
                self._event('system', 'flag_done', f"Flag {arg} is now {'ON' if value else 'OFF'} everywhere", '', arg=cause)
            elif kind == 'raise_timeout':
                self.config['tax_timeout_ms'] = 3000
                self._event('system', 'config_done', 'Tax client timeout is now 3000 ms', '', arg=cause)
            elif kind == 'scale':
                self.config['replicas'] = 24
                self._event('system', 'scale_done', 'checkout-api is running 24 pods', '', arg=cause)
            elif kind == 'restart':
                self._event('system', 'restart_done', 'Rolling restart finished', '', arg=cause)
            elif kind == 'failover_payments':
                self.config['payments'] = 'backup'
                self._event('system', 'failover_done', 'Payments now route to the backup provider',
                            'Backup provider fees are 0.4% higher per transaction.', arg=cause)
            elif kind == 'page_tax':
                if mode_for(self.config) == 'tax_slow':
                    reply = ('Tax team: /v2/quote has been saturated since about 14:03. Checkout v2.14.0 sends 100% of traffic to it '
                             'and it was only load-tested at 10%. We cannot add capacity quickly. Can you turn off tax_v2_quotes?')
                else:
                    reply = ('Tax team: /v2/quote traffic has dropped and tax-service looks healthy now. We will fix capacity '
                             'before tax_v2_quotes is turned on again.')
                self._event('comms', 'tax_reply', 'Reply from the tax-service team', reply, arg=cause)

    # ---- commands -------------------------------------------------------------------------------------------
    def current(self) -> dict:
        return self.history[self.now - 1]

    def pending_kinds(self) -> set:
        return {p['kind'] if p['kind'] != 'flag' else f"flag:{p['arg']}" for p in self.pending}

    def available_actions(self) -> dict:
        busy = self.pending_kinds()
        rollout = 'rollback' in busy or 'roll_forward' in busy
        version = self.config['version']
        reasons = {
            'rollback': 'A rollout is in progress.' if rollout else ('Already on v2.13.2.' if version == S.OLD else ''),
            'roll_forward': 'A rollout is in progress.' if rollout else ('Already on v2.14.0.' if version == S.NEW else ''),
            'flag': '',
            'raise_timeout': 'Already 3000 ms.' if self.config['tax_timeout_ms'] >= 3000 or 'raise_timeout' in busy else '',
            'scale': 'Already 24 pods.' if self.config['replicas'] > 12 or 'scale' in busy else '',
            'restart': 'A restart is in progress.' if 'restart' in busy else '',
            'failover_payments': 'Already on the backup provider.' if self.config['payments'] == 'backup' or 'failover_payments' in busy else '',
            'page_tax': 'Already paged.' if self.paged_tax else '',
            'status': '',
        }
        return {name: {'label': spec['label'], 'delay': spec['delay'], 'disabled': reasons[name]}
                for name, spec in ACTIONS.items()}

    def look(self, tool: str, arg: str | None = None) -> dict:
        self._require_open()
        if tool == 'metrics':
            if arg not in PANELS:
                raise CommandError('Unknown metrics panel.')
            view = self._metrics_view(arg)
            label = f'Viewed {PANELS[arg]} metrics'
        elif tool == 'logs':
            if arg not in LOG_SERVICES:
                raise CommandError('Unknown service.')
            view = self._logs_view(arg)
            label = f'Read {arg} logs'
        elif tool == 'deploys':
            view = {'releases': [{k: r[k] for k in ('version', 'service', 'when', 'author', 'title', 'status')}
                                 for r in S.releases(self.variant)]}
            label = 'Opened deploy history'
        elif tool == 'release':
            match = [r for r in S.releases(self.variant) if r['version'] == arg]
            if not match:
                raise CommandError('Unknown release.')
            view = match[0]
            label = f'Read release notes for {arg}'
        elif tool == 'flags':
            view = {'flags': [{'name': name, 'on': self.config['flags'][name], 'rollout': info.get('rollout', 100),
                               'description': info['description'], 'changed': info['changed']}
                              for name, info in S.FLAGS.items()]}
            label = 'Opened feature flags'
        elif tool == 'runbook':
            view = {'sections': [{'title': t, 'steps': steps} for t, steps in S.RUNBOOK]}
            label = 'Read the checkout runbook'
        else:
            raise CommandError('Unknown tool.')
        event = self._event('look', tool, label, self._look_detail(tool, arg, view), arg=arg)
        self._advance(LOOK_MINUTES)
        return {'event': event['id'], 'tool': tool, 'arg': arg, 'view': view}

    def act(self, action: str, arg: str | None = None, text: str = '') -> dict:
        self._require_open()
        available = self.available_actions()
        if action not in available:
            raise CommandError('Unknown action.')
        if available[action]['disabled']:
            raise CommandError(available[action]['disabled'])
        spec = ACTIONS[action]
        if action == 'flag':
            if arg not in S.FLAGS:
                raise CommandError('Unknown feature flag.')
            if f'flag:{arg}' in self.pending_kinds():
                raise CommandError('That flag change is already rolling out.')
            turning = 'OFF' if self.config['flags'][arg] else 'ON'
            event = self._event('action', 'flag', f'Turned {arg} {turning}', 'Takes about 1 minute to reach every pod.', arg=arg)
        elif action == 'status':
            text = ' '.join((text or S.STATUS_TEMPLATE).split())[:400]
            event = self._event('comms', 'status', 'Posted a status update', text)
        else:
            detail = f"Takes about {spec['delay']} minutes." if spec['delay'] else ''
            event = self._event('action', action, spec['label'], detail)
        if action == 'page_tax':
            self.paged_tax = True
        if action == 'restart':
            self.restart_until = self.now + spec['delay']
        if action != 'status':
            self.pending.append({'due': self.now + spec['delay'], 'kind': action, 'arg': arg, 'event': event})
        self._advance(ACTION_MINUTES)
        return {'event': event['id']}

    def wait(self) -> dict:
        self._require_open()
        event = self._event('wait', 'wait', f'Watched the dashboards for {WAIT_MINUTES} minutes')
        self._advance(WAIT_MINUTES)
        return {'event': event['id']}

    def resolve(self) -> dict:
        self._require_open()
        rate = self.current()['error_rate']
        event = self._event('action', 'resolve', 'Declared the incident resolved', f'Error rate at that moment: {rate}%')
        self.ended = 'resolved'
        return {'event': event['id']}

    def _require_open(self):
        if self.ended:
            raise CommandError('This shift has ended.')

    # ---- views ----------------------------------------------------------------------------------------------
    def _series(self, key: str) -> list:
        return [[minute, self.history[minute][key]] for minute in sorted(self.history)]

    def _metrics_view(self, panel: str) -> dict:
        charts = {
            'checkout': [('error_rate', '5xx error rate', '%'), ('p99_ms', 'p99 latency', 'ms')],
            'tax': [('tax_p99_ms', '/quote p99 latency', 'ms'), ('tax_busy_pct', 'Worker pool busy', '%')],
            'payments': [('pay_success_pct', 'Authorization success', '%'), ('pay_p95_ms', 'PayRail p95 latency', 'ms')],
            'database': [('db_cpu_pct', 'CPU', '%'), ('db_errors', 'Query errors per minute', '')],
        }[panel]
        return {'panel': panel, 'service': PANELS[panel], 'deploy_minute': S.DEPLOY,
                'charts': [{'key': key, 'title': title, 'unit': unit, 'series': self._series(key),
                            'latest': self.history[self.now - 1][key]} for key, title, unit in charts]}

    def _look_detail(self, tool: str, arg, view: dict) -> str:
        if tool == 'metrics':
            return ' · '.join(f"{c['title']} {c['latest']}{c['unit']}" for c in view['charts'])
        if tool == 'logs':
            errors = [line for line in view['lines'] if line['level'] == 'ERROR']
            return f"{len(errors)} errors in the last {len(view['lines'])} lines" + (f": {errors[-1]['message'][:90]}" if errors else '')
        if tool == 'release':
            migration = view.get('migration')
            if migration:
                return f"Migration {migration['name']}: {'reversible' if migration['reversible'] else 'IRREVERSIBLE'}"
            return 'No database migration'
        if tool == 'flags':
            return ', '.join(f"{f['name']} {'ON' if f['on'] else 'OFF'}" for f in view['flags'])
        return ''

    def _logs_view(self, service: str) -> dict:
        lines = []
        for minute in range(max(S.HISTORY_START, self.now - 4), self.now):
            lines.extend(_log_lines(service, minute, self.history[minute], self.configs[minute], self.variant))
        if service == 'orders-db' and self.now - 8 <= S.DEPLOY < self.now:
            lines.append(_line(S.DEPLOY, 11, 'INFO', f"migration {S.MIGRATION[self.variant]['name']} applied in 3.1s: "
                               + S.MIGRATION[self.variant]['summary']))
        if service == 'checkout-api' and self.now - 8 <= S.DEPLOY < self.now:
            lines.append(_line(S.DEPLOY, 40, 'INFO', 'rollout complete: 12/12 pods on v2.14.0'))
        lines.sort(key=lambda line: (line['minute'], line['second']))
        return {'service': service, 'lines': lines[-16:]}

    def view(self) -> dict:
        cur = self.current()
        status = 'ended' if self.ended else ('mitigated' if self.healthy_streak >= S.HEALTHY_STREAK
                                             else 'firing' if cur['error_rate'] > S.ALERT_THRESHOLD else 'recovering')
        return {'variant': self.variant, 'title': S.VARIANT_INFO[self.variant]['title'],
                'briefing': S.VARIANT_INFO[self.variant]['briefing'],
                'clock': S.clock(self.now), 'minute': self.now, 'elapsed': self.now - S.START,
                'limit': S.SHIFT_LIMIT, 'status': status, 'ended': self.ended,
                'error_rate': cur['error_rate'], 'p99_ms': cur['p99_ms'], 'failed': round(self.failed),
                'alert': {'opened': S.clock(S.ALERT), 'threshold': S.ALERT_THRESHOLD},
                'pending': [{'kind': p['kind'], 'arg': p['arg'], 'label': p['event']['label'], 'due': S.clock(p['due']),
                             'minutes_left': p['due'] - self.now} for p in self.pending],
                'actions': self.available_actions(),
                'flags': {name: on for name, on in self.config['flags'].items()},
                'events': self.events}


def _line(minute: int, second: int, level: str, message: str) -> dict:
    return {'minute': minute, 'second': second, 'time': f'{S.clock(minute)}:{second:02d}', 'level': level, 'message': message}


def _order(minute: int, i: int) -> str:
    return 'ord_' + format(zlib.crc32(f'{minute}-{i}'.encode()) % 36**5, 'x')[:5]


def _log_lines(service: str, minute: int, m: dict, config: dict, variant: str) -> list:
    mode, out = m['mode'], []
    s = lambda i: (minute * 7 + i * 13) % 60
    if service == 'checkout-api':
        if mode == 'tax_slow' and config['tax_timeout_ms'] < 3000:
            out.append(_line(minute, s(1), 'ERROR', f'POST /checkout 503 tax_quote_failed: TimeoutError: tax-service /v2/quote exceeded 800ms ({_order(minute, 1)})'))
            out.append(_line(minute, s(2), 'ERROR', f'POST /checkout 503 tax_quote_failed: TimeoutError: tax-service /v2/quote exceeded 800ms ({_order(minute, 2)})'))
            out.append(_line(minute, s(3), 'INFO', f'POST /checkout 200 812ms ({_order(minute, 3)})'))
        elif mode == 'tax_slow':
            out.append(_line(minute, s(1), 'WARN', f'tax-service /v2/quote slow: 2410ms ({_order(minute, 1)})'))
            out.append(_line(minute, s(2), 'INFO', f'POST /checkout 200 2.6s ({_order(minute, 2)})'))
        elif mode == 'broken_schema':
            out.append(_line(minute, s(1), 'ERROR', f'POST /checkout 500 psycopg.errors.UndefinedColumn: column "tax_amount" of relation "orders" does not exist ({_order(minute, 1)})'))
            out.append(_line(minute, s(2), 'ERROR', f'POST /checkout 500 psycopg.errors.UndefinedColumn: column "tax_amount" of relation "orders" does not exist ({_order(minute, 2)})'))
        else:
            out.append(_line(minute, s(1), 'INFO', f'POST /checkout 200 380ms ({_order(minute, 1)})'))
        if minute % 2 == 0:
            out.append(_line(minute, s(4), 'WARN', f'payment authorization declined: card_declined ({_order(minute, 4)})'))
    elif service == 'tax-service':
        if mode == 'tax_slow':
            out.append(_line(minute, s(1), 'WARN', f"/v2/quote p99 {m['tax_p99_ms']}ms: worker pool saturated (64/64 busy), queue depth 380"))
            out.append(_line(minute, s(2), 'WARN', 'request cancelled by client after 800ms' if config['tax_timeout_ms'] < 3000 else '/v2/quote 200 2390ms'))
        else:
            out.append(_line(minute, s(1), 'INFO', '/v1/quote 200 41ms'))
    elif service == 'payments-gateway':
        out.append(_line(minute, s(1), 'WARN', 'PayRail: elevated latency in eu-west (p95 640ms); provider status: degraded since 13:20'
                         if config['payments'] == 'primary' else 'routing to backup provider: authorize ok 498ms'))
        out.append(_line(minute, s(2), 'INFO', 'authorize ok 312ms'))
    elif service == 'orders-db':
        if mode == 'broken_schema':
            out.append(_line(minute, s(1), 'ERROR', 'ERROR: column "tax_amount" of relation "orders" does not exist at character 58 (INSERT INTO orders ...)'))
        out.append(_line(minute, s(2), 'INFO', f"connections {180 + round(8 * wobble(minute, 9))}/400, checkpoint complete"))
    return out
