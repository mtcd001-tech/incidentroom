"""Spending guard tests. They use a temporary database and never call a model."""
import datetime as dt
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import budget

MODEL = 'nvidia/Nemotron-3.5-Super-VL'
USAGE = {'prompt_tokens': 1000, 'completion_tokens': 100}   # $0.00039 at Super prices


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = Path(self.dir.name) / 'budget.db'
        self.env = mock.patch.dict(os.environ, {'BUDGET_DB': str(self.db), 'BUDGET_TOTAL_USD': '1', 'BUDGET_JUDGE_USD': '0.2',
                                                'BUDGET_END_DATE': '2026-11-06', 'VISITOR_DAILY_CALLS': '5'})
        self.env.start()
        self.day = mock.patch.object(budget, 'today', return_value=dt.date(2026, 10, 28))
        self.day.start()
        budget._inflight.clear()

    def tearDown(self):
        self.day.stop()
        self.env.stop()
        self.dir.cleanup()

    def call(self, pool='public', visitor='v1', cost=0.001, usage=USAGE):
        spend = budget.Spend(pool, visitor)
        ticket = spend.reserve(MODEL, cost)
        spend.finish(ticket, MODEL, usage)
        return ticket

    def rows(self):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute('SELECT pool, reserved, actual, status FROM spend ORDER BY id').fetchall()
        finally:
            conn.close()

    def test_missing_or_broken_record_turns_paid_calls_off(self):
        with self.assertRaises(budget.BudgetError):
            self.call()
        self.assertFalse(self.db.exists())   # the server never creates a fresh budget
        self.db.write_text('not a database')
        with self.assertRaises(budget.BudgetError):
            self.call()

    def test_init_refuses_to_overwrite(self):
        budget.init(0.01)
        with self.assertRaises(SystemExit):
            budget.init(0.0)

    def test_settles_from_real_usage_and_keeps_uncertain_reservations(self):
        budget.init(0.0)
        self.call()
        self.call(visitor='v2', usage=None)
        rows = self.rows()
        self.assertEqual(rows[0][3], 'settled')
        self.assertAlmostEqual(rows[0][2], 0.00039)
        self.assertEqual(rows[1][3], 'uncertain')
        self.assertEqual(budget.report()['spent'], round(0.00039 + 0.001, 4))

    def test_public_daily_allowance(self):
        # public pool = 1 - 0.2 judge - 0.1 already spent = 0.7 over 10 days (Oct 28..Nov 6) → 0.07 today
        budget.init(0.1)
        self.call(cost=0.05, usage=None)
        with self.assertRaises(budget.BudgetError) as err:
            self.call(visitor='v2', cost=0.03)
        self.assertIn("today's limit", str(err.exception))
        self.call(visitor='v2', cost=0.015, usage=None)
        with mock.patch.object(budget, 'today', return_value=dt.date(2026, 10, 29)):
            self.call(visitor='v3', cost=0.07, usage=None)   # (0.7 - 0.065) / 9 days ≈ 0.0706

    def test_judge_pool_is_separate_but_capped(self):
        budget.init(0.0)
        with mock.patch.object(budget, 'today', return_value=dt.date(2026, 10, 28)):
            self.call(cost=0.08, usage=None)               # uses up today's public allowance (0.08)
            with self.assertRaises(budget.BudgetError):
                self.call(visitor='v2', cost=0.01)
            self.call(pool='judge', visitor='j', cost=0.15, usage=None)
            with self.assertRaises(budget.BudgetError) as err:
                self.call(pool='judge', visitor='j', cost=0.1)
            self.assertIn('judge budget', str(err.exception))

    def test_total_cap_includes_development_spending(self):
        budget.init(0.95)
        with self.assertRaises(budget.BudgetError) as err:
            self.call(pool='judge', visitor='j', cost=0.06)
        self.assertIn('whole budget', str(err.exception))

    def test_per_visitor_daily_calls_count_failures(self):
        budget.init(0.0)
        for _ in range(5):
            self.call(cost=0.0001, usage=None)
        with self.assertRaises(budget.BudgetError) as err:
            self.call(cost=0.0001)
        self.assertIn('all AI messages for today', str(err.exception))
        self.call(visitor='v2', cost=0.0001)

    def test_one_call_at_a_time_per_visitor(self):
        budget.init(0.0)
        first = budget.Spend('public', 'v1')
        ticket = first.reserve(MODEL, 0.001)
        with self.assertRaises(budget.BudgetError):
            budget.Spend('public', 'v1').reserve(MODEL, 0.001)
        self.call(visitor='v2')
        first.finish(ticket, MODEL, USAGE)
        self.call(visitor='v1')

    def test_refused_reservation_releases_the_visitor(self):
        budget.init(0.0)
        with self.assertRaises(budget.BudgetError):
            self.call(cost=5)
        self.call()

    def test_worst_case_cost_is_above_real_cost(self):
        messages = [{'role': 'system', 'content': 'x' * 4000}, {'role': 'user', 'content': 'Should I roll back?'}]
        worst = budget.max_cost(MODEL, messages, 220)
        real = budget.usage_cost(MODEL, {'prompt_tokens': 1010, 'completion_tokens': 220})
        self.assertGreater(worst, real)
        self.assertEqual(budget.price('some/unknown-model'), budget.FALLBACK_PRICE)

    def test_worst_case_covers_token_dense_text(self):
        dense = '漢字🔥' * 200 + ''.join(chr(0x2600 + i % 200) for i in range(400))
        messages = [{'role': 'user', 'content': dense}]
        # Even one token per character (worse than any real tokenizer for this text) stays under the bound.
        real = budget.usage_cost(MODEL, {'prompt_tokens': len(dense) + 64, 'completion_tokens': 220})
        self.assertGreater(budget.max_cost(MODEL, messages, 220), real)


if __name__ == '__main__':
    unittest.main(verbosity=2)
