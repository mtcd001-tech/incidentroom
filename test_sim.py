import unittest

import facts
import scenario as S
import sim


def run(variant, commands):
    inc = sim.Incident(variant)
    for command in commands:
        kind, *rest = command
        if kind == 'look':
            inc.look(*rest)
        elif kind == 'act':
            inc.act(*rest)
        elif kind == 'wait':
            inc.wait()
        elif kind == 'resolve':
            inc.resolve()
    return inc


def waits(n):
    return [('wait',)] * n


class SimulationTests(unittest.TestCase):
    def test_starts_firing_at_about_21_percent(self):
        inc = sim.Incident('A')
        view = inc.view()
        self.assertEqual(view['clock'], '14:06')
        self.assertEqual(view['status'], 'firing')
        self.assertGreater(view['error_rate'], 18)
        self.assertLess(inc.history[S.DEPLOY - 1]['error_rate'], 1)

    def test_same_commands_give_identical_timelines(self):
        plan = [('look', 'metrics', 'checkout'), ('act', 'rollback'), *waits(3)]
        a, b = run('A', plan), run('A', plan)
        self.assertEqual(a.events, b.events)
        self.assertEqual(a.history, b.history)

    def test_rollback_fixes_variant_a(self):
        inc = run('A', [('act', 'rollback'), *waits(4)])
        self.assertEqual(inc.config['version'], S.OLD)
        self.assertLess(inc.current()['error_rate'], 1)
        self.assertEqual(inc.view()['status'], 'mitigated')

    def test_rollback_breaks_every_checkout_in_variant_b(self):
        inc = run('B', [('act', 'rollback'), *waits(3)])
        self.assertEqual(inc.current()['mode'], 'broken_schema')
        self.assertGreater(inc.current()['error_rate'], 95)
        lines = inc.look('logs', 'checkout-api')['view']['lines']
        self.assertTrue(any('UndefinedColumn' in line['message'] for line in lines))

    def test_variant_b_recovers_by_rolling_forward_and_turning_the_flag_off(self):
        inc = run('B', [('act', 'rollback'), *waits(3), ('act', 'roll_forward'), ('act', 'flag', 'tax_v2_quotes'), *waits(4)])
        self.assertEqual(inc.config['version'], S.NEW)
        self.assertFalse(inc.config['flags']['tax_v2_quotes'])
        self.assertIsNotNone(inc.mitigated_at)

    def test_flag_kill_switch_works_in_both_variants(self):
        for variant in S.VARIANTS:
            inc = run(variant, [('act', 'flag', 'tax_v2_quotes'), *waits(2)])
            self.assertLess(inc.current()['error_rate'], 1, variant)

    def test_scaling_makes_it_worse_and_timeout_only_partly_helps(self):
        scaled = run('A', [('act', 'scale'), *waits(2)])
        self.assertGreater(scaled.current()['error_rate'], 24)
        timeout = run('A', [('act', 'raise_timeout'), *waits(2)])
        self.assertTrue(1 < timeout.current()['error_rate'] < 4)
        self.assertGreater(timeout.current()['p99_ms'], 2500)

    def test_unrelated_changes_do_not_help(self):
        for action in (('act', 'failover_payments'), ('act', 'flag', 'new_cart_ui'), ('act', 'restart')):
            inc = run('A', [action, *waits(3)])
            self.assertGreater(inc.current()['error_rate'], 18, action)

    def test_actions_are_blocked_while_busy_or_done(self):
        inc = run('A', [('act', 'rollback')])
        with self.assertRaises(sim.CommandError):
            inc.act('rollback')
        with self.assertRaises(sim.CommandError):
            inc.act('roll_forward')
        inc.act('page_tax')
        with self.assertRaises(sim.CommandError):
            inc.act('page_tax')
        with self.assertRaises(sim.CommandError):
            inc.act('flag', 'no_such_flag')

    def test_tax_team_reply_depends_on_state(self):
        inc = run('A', [('act', 'page_tax'), *waits(3)])
        reply = next(e for e in inc.events if e['name'] == 'tax_reply')
        self.assertIn('turn off tax_v2_quotes', reply['detail'])
        fixed = run('A', [('act', 'flag', 'tax_v2_quotes'), ('act', 'page_tax'), *waits(3)])
        reply = next(e for e in fixed.events if e['name'] == 'tax_reply')
        self.assertIn('looks healthy', reply['detail'])

    def test_every_look_and_action_costs_time(self):
        inc = sim.Incident('A')
        inc.look('runbook')
        inc.act('status', text='Investigating checkout errors.')
        inc.wait()
        self.assertEqual(inc.now, S.START + 4)

    def test_shift_limit_and_resolve_end_the_run(self):
        inc = run('A', waits(S.SHIFT_LIMIT // sim.WAIT_MINUTES))
        self.assertEqual(inc.ended, 'time')
        with self.assertRaises(sim.CommandError):
            inc.wait()
        resolved = run('A', [('resolve',)])
        self.assertEqual(resolved.ended, 'resolved')

    def test_release_notes_differ_only_in_the_migration(self):
        a = sim.Incident('A').look('release', S.NEW)['view']['migration']
        b = sim.Incident('B').look('release', S.NEW)['view']['migration']
        self.assertTrue(a['reversible'])
        self.assertFalse(b['reversible'])

    def test_payments_warning_predates_the_deploy(self):
        inc = sim.Incident('A')
        lines = inc.look('logs', 'payments-gateway')['view']['lines']
        self.assertTrue(any('since 13:20' in line['message'] for line in lines))
        self.assertLess(S.PAYMENTS_WARNING, S.DEPLOY)

    def test_unknown_inputs_are_rejected(self):
        inc = sim.Incident('A')
        for call in (lambda: inc.look('metrics', 'nope'), lambda: inc.look('shell'), lambda: inc.act('reboot_world'),
                     lambda: sim.Incident('C')):
            with self.assertRaises(sim.CommandError):
                call()


class FactTests(unittest.TestCase):
    def assert_refs_exist(self, inc, summary):
        ids = {e['id'] for e in inc.events}
        for fact in summary['facts']:
            for ref in fact['events']:
                self.assertIn(ref, ids, fact['text'])

    def test_b_rollback_is_reported_with_evidence(self):
        inc = run('B', [('look', 'metrics', 'checkout'), ('act', 'rollback'), *waits(3), ('act', 'roll_forward'),
                        ('act', 'flag', 'tax_v2_quotes'), *waits(5), ('resolve',)])
        summary = facts.summarize(inc)
        self.assert_refs_exist(inc, summary)
        texts = ' '.join(f['text'] for f in summary['facts'])
        self.assertIn('without opening the v2.14.0 release notes', texts)
        self.assertIn('broke almost every checkout', texts)
        self.assertIn('declared it resolved', texts)
        self.assertEqual(summary['stats']['unhelpful_changes'], 1)

    def test_reading_release_notes_first_is_credited(self):
        inc = run('A', [('look', 'deploys'), ('look', 'release', S.NEW), ('act', 'rollback'), *waits(4)])
        summary = facts.summarize(inc)
        self.assert_refs_exist(inc, summary)
        good = [f['text'] for f in summary['facts'] if f['tone'] == 'good']
        self.assertTrue(any('read the v2.14.0 release notes before rolling back' in t for t in good))
        self.assertTrue(any('healthy' in t and 'Roll back' in t for t in good))

    def test_acting_blind_and_silence_are_gaps(self):
        inc = run('A', [('act', 'scale'), *waits(3), ('resolve',)])
        summary = facts.summarize(inc)
        gaps = ' '.join(f['text'] for f in summary['facts'] if f['tone'] == 'gap')
        self.assertIn('before you looked at any', gaps)
        self.assertIn('did not post a status update', gaps)
        self.assertIn('error rate rose', gaps)
        self.assertIn('while the error rate was', gaps)

    def test_compare_uses_recorded_stats(self):
        a = facts.summarize(run('A', [('act', 'rollback'), *waits(4), ('resolve',)]))
        b = facts.summarize(run('B', [('look', 'flags'), ('act', 'flag', 'tax_v2_quotes'), *waits(3), ('resolve',)]))
        rows = facts.compare(a, b)
        self.assertEqual(rows[0]['label'], 'Time to healthy checkout')
        self.assertEqual(rows[2]['first'], f'Roll back checkout-api to {S.OLD} (0 min in)')
        self.assertEqual(rows[3]['second'], '1')


if __name__ == '__main__':
    unittest.main(verbosity=2)
