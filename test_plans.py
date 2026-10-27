"""Plan validation and execution tests. Deterministic; no model calls."""
import unittest

import plans
import scenario as S

ROLLBACK = {'steps': [{'act': 'rollback'}, {'wait': True}, {'wait': True}]}
CHECK_FIRST = {'steps': [
    {'look': 'release', 'arg': S.NEW},
    {'if': 'migration_irreversible',
     'then': [{'act': 'flag', 'arg': 'tax_v2_quotes', 'to': 'off'}, {'wait': True}],
     'else': [{'act': 'rollback'}, {'wait': True}]},
]}


class ValidationTests(unittest.TestCase):
    def test_accepts_a_plan_and_keeps_notes(self):
        plan = plans.validate(dict(CHECK_FIRST, assumptions=['"check the release" = open the v2.14.0 notes'],
                                   unsupported=['Call my manager'], extra='ignored'))
        self.assertEqual(len(plan['steps']), 2)
        self.assertEqual(plan['unsupported'], ['Call my manager'])
        lines = [line['text'] for line in plans.outline(plan)]
        self.assertEqual(lines[0], f'Look at release notes for {S.NEW}')
        self.assertIn('Otherwise:', lines)

    def test_unrequested_status_updates_are_left_out(self):
        added = plans.validate({'steps': [CHECK_FIRST['steps'][0], {'act': 'status'}, CHECK_FIRST['steps'][1], {'act': 'status'}]})
        plan = plans.drop_unrequested_status(added, 'Read the notes, then decide. Then call my manager.')
        self.assertFalse([s for s in plan['steps'] if s.get('act') == 'status'])
        self.assertIn('Left out a status update', plan['assumptions'][-1])
        kept = plans.drop_unrequested_status(added, 'Read the notes, decide, and post a status update.')
        self.assertIs(kept, added)
        with self.assertRaises(plans.PlanError):
            plans.drop_unrequested_status(plans.validate({'steps': [{'act': 'status'}]}), 'call my boss')

    def test_rejects_what_the_simulator_cannot_run(self):
        bad = [
            {},
            {'steps': []},
            {'steps': [{'act': 'reboot_world'}]},
            {'steps': [{'act': 'resolve'}]},
            {'steps': [{'look': 'metrics', 'arg': 'kitchen'}]},
            {'steps': [{'act': 'flag', 'arg': 'tax_v2_quotes'}]},
            {'steps': [{'if': 'error_rate_above_2', 'then': [{'wait': True}]}]},
            {'steps': [{'if': 'migration_irreversible', 'then': [{'if': 'migration_irreversible', 'then': [{'wait': True}]}]}]},
            {'steps': [CHECK_FIRST['steps'][1], CHECK_FIRST['steps'][1]]},
            {'steps': [{'wait': True}] * 9},
            {'steps': [{'wait': True}] * 6 + [{'if': 'migration_irreversible', 'then': [{'wait': True}] * 3}]},
            {'steps': ['roll back']},
            {'steps': [{'look': []}]},
            {'steps': [{'look': 'metrics', 'arg': ['checkout']}]},
            {'steps': [{'act': {'x': 1}}]},
            {'steps': [{'act': 'flag', 'arg': ['tax_v2_quotes'], 'to': 'off'}]},
            {'steps': [{'if': {'a': 1}, 'then': [{'wait': True}]}]},
        ]
        for raw in bad:
            with self.assertRaises(plans.PlanError, msg=raw):
                plans.validate(raw)


class ExecutionTests(unittest.TestCase):
    def test_always_roll_back_works_in_a_and_breaks_b(self):
        result = plans.stress_test(plans.validate(ROLLBACK))
        a, b = result['A']['stats'], result['B']['stats']
        self.assertEqual(plans.verdict(a), 'recovered and stayed healthy')
        self.assertEqual(plans.verdict(b), 'broke checkout')
        self.assertGreater(b['unsafe_minutes'], 0)
        self.assertGreater(b['failed'], a['failed'])
        self.assertEqual(result['comparison'][0]['second'], 'broke checkout')

    def test_check_first_plan_survives_both(self):
        result = plans.stress_test(plans.validate(CHECK_FIRST))
        for variant in 'AB':
            stats = result[variant]['stats']
            self.assertEqual(plans.verdict(stats), 'recovered and stayed healthy', variant)
            self.assertEqual(stats['unsafe_minutes'], 0)
        self.assertIn('first branch', result['B']['trace'][1]['text'])
        self.assertIn('second branch', result['A']['trace'][1]['text'])

    def test_decision_without_evidence_stops_the_plan(self):
        plan = plans.validate({'steps': CHECK_FIRST['steps'][1:]})
        result = plans.execute(plan, 'B')
        self.assertIn('had not opened', result['stopped'])
        self.assertTrue(result['stats']['stopped'])
        self.assertEqual(plans.verdict(result['stats']), 'did not recover')

    def test_both_variants_use_the_same_window(self):
        short = plans.execute(plans.validate({'steps': [{'act': 'status'}]}), 'A')
        long = plans.execute(plans.validate(CHECK_FIRST), 'A')
        last = lambda r: max(e['minute'] for e in r['events']) if r['events'] else S.START
        self.assertEqual(short['stats']['window'], long['stats']['window'])
        self.assertLessEqual(last(short), S.START + plans.WINDOW)
        self.assertGreater(short['stats']['failed'], long['stats']['failed'])

    def test_facts_are_scoped_per_variant_and_runs_are_deterministic(self):
        plan = plans.validate(ROLLBACK)
        first, second = plans.stress_test(plan), plans.stress_test(plan)
        self.assertEqual(first['B']['stats'], second['B']['stats'])
        self.assertTrue(all(f['id'].startswith('B-F') for f in first['B']['facts']))

    def test_flag_steps_set_a_state_and_skip_when_already_there(self):
        plan = plans.validate({'steps': [{'act': 'flag', 'arg': 'tax_v2_quotes', 'to': 'off'},
                                         {'act': 'flag', 'arg': 'tax_v2_quotes', 'to': 'off'}]})
        result = plans.execute(plan, 'A')
        self.assertIn('Skipped', result['trace'][1]['note'])

    def test_opposite_flag_request_during_rollout_stops_the_plan(self):
        plan = plans.validate({'steps': [{'act': 'flag', 'arg': 'tax_v2_quotes', 'to': 'off'},
                                         {'act': 'flag', 'arg': 'tax_v2_quotes', 'to': 'on'}]})
        result = plans.execute(plan, 'A')
        self.assertIn('still rolling out', result['stopped'])
        self.assertTrue(result['stats']['stopped'])

    def test_removing_a_status_step_keeps_the_other_branch(self):
        plan = plans.validate({'steps': [{'look': 'release', 'arg': S.NEW},
                                         {'if': 'migration_irreversible', 'then': [{'act': 'status'}], 'else': [{'act': 'rollback'}]}]})
        kept = plans.drop_unrequested_status(plan, 'Read the notes. If irreversible call my manager, otherwise roll back.')
        decision = kept['steps'][1]
        self.assertEqual(decision['then'], [])
        self.assertEqual(decision['else'], [{'act': 'rollback', 'arg': None}])
        self.assertIn('Do nothing more', [line['text'] for line in plans.outline(kept)])
        result = plans.execute(kept, 'A')
        self.assertEqual(result['stats']['verdict'], 'recovered and stayed healthy')

    def test_a_step_the_sim_refuses_stops_with_a_reason(self):
        result = plans.execute(plans.validate({'steps': [{'act': 'roll_forward'}]}), 'A')
        self.assertIn('could not run', result['stopped'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
