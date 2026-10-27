"""Authored content for the checkout incident. Everything here is fictional.

Times are minutes since midnight on the incident day. Both variants share the same symptoms;
only release v2.14.0's database migration differs, and that changes what a rollback does.
"""

def clock(minute: int) -> str:
    return f'{minute // 60:02d}:{minute % 60:02d}'

COMPANY = 'Fernway Outdoor'
SERVICE = 'checkout-api'

HISTORY_START = 13 * 60 + 40   # 13:40, the earliest minute shown on charts
PAYMENTS_WARNING = 13 * 60 + 20  # PayRail eu-west latency warning began (red herring, before the deploy)
DEPLOY = 14 * 60 + 2            # v2.14.0 finished rolling out
INCIDENT = DEPLOY + 1           # first minute with tax v2 traffic
ALERT = 14 * 60 + 5             # page fired
START = 14 * 60 + 6             # your shift starts
SHIFT_LIMIT = 40                # minutes before the incident commander takes over

TRAFFIC = 850                   # checkouts per minute
ALERT_THRESHOLD = 2.0           # % 5xx
HEALTHY = 1.0                   # % 5xx counted as healthy
HEALTHY_STREAK = 3              # healthy minutes in a row = mitigated

OLD, NEW = 'v2.13.2', 'v2.14.0'
VARIANTS = ('A', 'B')

VARIANT_INFO = {
    'A': {
        'title': 'The page',
        'briefing': ('You are on call for checkout at Fernway Outdoor. At 14:05 you were paged: checkout is failing for '
                     'about one in five customers. Investigate, act, and get checkout healthy. Every look and every '
                     'action takes simulated time, and customers keep failing while you work.'),
    },
    'B': {
        'title': 'Pressure Replay: rollback is unsafe',
        'briefing': ('Same page, same symptoms, one change: this time release v2.14.0 shipped an irreversible database '
                     'migration. Rolling checkout back to v2.13.2 is not a safe way out. Find another way to stop the '
                     'failures.'),
    },
}

MIGRATION = {
    'A': {'name': '0042_tax_breakdown', 'summary': 'Add nullable column orders.tax_breakdown (JSONB)',
          'reversible': True, 'note': 'Backward compatible: v2.13.2 ignores the new column.'},
    'B': {'name': '0042_tax_breakdown', 'summary': 'Move orders.tax_amount into orders.tax_breakdown (JSONB) and drop tax_amount',
          'reversible': False, 'note': 'IRREVERSIBLE. v2.13.2 still writes orders.tax_amount, so it cannot run against this schema.'},
}

def releases(variant: str) -> list[dict]:
    migration = MIGRATION[variant]
    return [
        {'version': NEW, 'service': SERVICE, 'minute': DEPLOY, 'when': 'Today 14:02', 'author': 'deploy-bot (PR #1482)',
         'title': 'Tax v2 quotes for all regions', 'status': 'succeeded',
         'changes': ['Turn on feature flag tax_v2_quotes for 100% of traffic (uses tax-service /v2/quote)',
                     'Lower the tax client timeout from 3000 ms to 800 ms ("fail fast")',
                     f"Database migration {migration['name']}: {migration['summary']}"],
         'migration': migration},
        {'version': OLD, 'service': SERVICE, 'minute': None, 'when': 'Yesterday 16:40', 'author': 'deploy-bot (PR #1477)',
         'title': 'Fix coupon rounding for multi-currency carts', 'status': 'succeeded',
         'changes': ['Round coupon discounts per line item', 'No database migrations'], 'migration': None},
        {'version': 'tax-5.2.0', 'service': 'tax-service', 'minute': None, 'when': '3 days ago', 'author': 'deploy-bot (PR #311)',
         'title': 'Add /v2/quote endpoint with per-item tax rules', 'status': 'succeeded',
         'changes': ['New endpoint /v2/quote (per-item rules, one lookup per line item)',
                     'Load-tested at 10% of checkout traffic', '/v1/quote unchanged'], 'migration': None},
        {'version': 'v2.13.1', 'service': SERVICE, 'minute': None, 'when': '2 days ago', 'author': 'deploy-bot (PR #1470)',
         'title': 'Upgrade payment SDK', 'status': 'succeeded', 'changes': ['PayRail SDK 4.8 → 4.9'], 'migration': None},
    ]

FLAGS = {
    'tax_v2_quotes': {'description': 'Quote tax with tax-service /v2/quote (per-item rules). Off = legacy /v1/quote.',
                      'changed': 'Today 14:02 by deploy-bot (release v2.14.0)'},
    'express_pay_button': {'description': 'Show the express pay button on the cart page.', 'changed': '3 days ago by m.okafor'},
    'new_cart_ui': {'description': 'New cart layout for 25% of visitors.', 'changed': 'Last week by j.silva', 'rollout': 25},
}

RUNBOOK = [
    ('Checkout 5xx alert', [
        'Confirm the impact: checkout error rate and latency (Metrics → checkout).',
        'Look for a recent change: deploys (Deploys) and feature flags (Flags).',
        'Check dependencies: tax-service, payments-gateway, orders-db.',
    ]),
    ('Mitigate first, then find the root cause', [
        'Prefer the smallest change that stops the bleeding. A kill switch (feature flag) for new code is faster than a rollback.',
        'Before rolling back, open the release notes and check for database migrations. An irreversible migration makes rollback unsafe.',
        'Change one thing at a time and watch the metrics before the next change.',
    ]),
    ('Communicate', [
        'Post a status update within 10 minutes of the page, and again when it is mitigated.',
        'Page the owning team if a dependency looks unhealthy.',
    ]),
]

STATUS_TEMPLATE = ('Investigating: some customers cannot complete checkout since about 14:03. '
                   'We are working on a fix and will update within 15 minutes.')
