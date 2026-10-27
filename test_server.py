import hashlib
import os
import unittest
from unittest import mock

from fastapi.testclient import TestClient

import plans
import server
import teammate


class ServerTests(unittest.TestCase):
    def setUp(self):
        server.runs.clear()
        server._new_runs.clear()
        self.client = TestClient(server.app)

    def start(self, variant='A', previous=None):
        res = self.client.post('/api/runs', json={'variant': variant, 'previous': previous})
        self.assertEqual(res.status_code, 200)
        return res.json()

    def test_index_and_scenario(self):
        self.assertEqual(self.client.get('/').status_code, 200)
        info = self.client.get('/api/scenario').json()
        self.assertIn('irreversible', info['variants']['B']['briefing'])

    def test_full_run_then_replay_with_comparison(self):
        run = self.start()
        rid = run['id']
        self.assertEqual(run['view']['status'], 'firing')
        res = self.client.post(f'/api/runs/{rid}/look', json={'tool': 'metrics', 'arg': 'checkout'}).json()
        self.assertEqual(res['result']['view']['panel'], 'checkout')
        self.client.post(f'/api/runs/{rid}/act', json={'action': 'rollback'})
        for _ in range(4):
            self.client.post(f'/api/runs/{rid}/wait')
        done = self.client.post(f'/api/runs/{rid}/resolve').json()
        self.assertTrue(done['view']['ended'])
        self.assertIn('debrief', done)
        self.assertNotIn('comparison', done)

        replay = self.start('B', rid)
        self.assertEqual(replay['previous'], rid)
        bid = replay['id']
        self.client.post(f'/api/runs/{bid}/act', json={'action': 'flag', 'arg': 'tax_v2_quotes'})
        for _ in range(3):
            self.client.post(f'/api/runs/{bid}/wait')
        final = self.client.post(f'/api/runs/{bid}/resolve').json()
        self.assertEqual(len(final['comparison']), 6)

    def test_bad_commands_are_400_and_unknown_runs_404(self):
        rid = self.start()['id']
        self.assertEqual(self.client.post(f'/api/runs/{rid}/act', json={'action': 'flag', 'arg': 'nope'}).status_code, 400)
        self.assertEqual(self.client.post(f'/api/runs/{rid}/look', json={'tool': 'shell'}).status_code, 422)
        self.assertEqual(self.client.get('/api/runs/does-not-exist').status_code, 404)
        self.client.post(f'/api/runs/{rid}/resolve')
        self.assertEqual(self.client.post(f'/api/runs/{rid}/wait').status_code, 400)

    def test_run_store_is_bounded(self):
        old_max = server.MAX_RUNS
        server.MAX_RUNS = 3
        try:
            for _ in range(6):
                self.start()
            self.assertLessEqual(len(server.runs), 3)
        finally:
            server.MAX_RUNS = old_max

    def test_chat_records_messages_without_using_simulated_time(self):
        run = self.start()
        rid, clock = run['id'], run['view']['clock']
        self.assertEqual(run['chat'], [])
        self.assertEqual(run['chat_left'], server.MAX_CHAT)
        answer = {'text': 'What have you looked at so far?', 'usage': {}, 'seconds': 0.5}
        with mock.patch.object(teammate, 'reply', return_value=answer) as reply:
            res = self.client.post(f'/api/runs/{rid}/chat', json={'text': '  Should I   roll back? '}).json()
        reply.assert_called_once()
        self.assertEqual([m['role'] for m in res['chat']], ['trainee', 'teammate'])
        self.assertEqual(res['chat'][0]['text'], 'Should I roll back?')
        self.assertEqual(res['view']['clock'], clock)
        self.assertEqual(res['view']['events'], [])
        self.assertEqual(res['chat_left'], server.MAX_CHAT - 1)

    def test_chat_limits_and_failures(self):
        rid = self.start()['id']
        with mock.patch.object(teammate, 'reply', side_effect=teammate.TeammateError('down')):
            res = self.client.post(f'/api/runs/{rid}/chat', json={'text': 'hi'})
        self.assertEqual(res.status_code, 503)
        self.assertEqual(self.client.get(f'/api/runs/{rid}').json()['chat'], [])
        self.assertEqual(self.client.post(f'/api/runs/{rid}/chat', json={'text': ''}).status_code, 422)
        answer = {'text': 'Why?', 'usage': {}, 'seconds': 0.1}
        with mock.patch.object(teammate, 'reply', return_value=answer) as reply:
            for _ in range(server.MAX_CHAT):
                self.assertEqual(self.client.post(f'/api/runs/{rid}/chat', json={'text': 'hi'}).status_code, 200)
            self.assertEqual(self.client.post(f'/api/runs/{rid}/chat', json={'text': 'hi'}).status_code, 429)
            self.assertEqual(reply.call_count, server.MAX_CHAT)
        self.client.post(f'/api/runs/{rid}/resolve')
        self.assertEqual(self.client.post(f'/api/runs/{rid}/chat', json={'text': 'hi'}).status_code, 400)
    def test_interpretation_is_made_once_and_attempts_are_capped(self):
        rid = self.start()['id']
        self.assertEqual(self.client.post(f'/api/runs/{rid}/interpret').status_code, 400)
        done = self.client.post(f'/api/runs/{rid}/resolve').json()
        self.assertIsNone(done['interpretation'])
        self.assertEqual(done['interpret_left'], server.MAX_INTERPRET)
        result = {'points': [{'kind': 'change', 'text': 'Post a status update.', 'facts': ['F1']}], 'dropped': 0}
        with mock.patch.object(teammate, 'interpret', return_value=result) as interpret:
            first = self.client.post(f'/api/runs/{rid}/interpret').json()
            again = self.client.post(f'/api/runs/{rid}/interpret').json()
        interpret.assert_called_once()
        self.assertEqual(first['interpretation'], result)
        self.assertEqual(again['interpretation'], result)

        other = self.start()['id']
        self.client.post(f'/api/runs/{other}/resolve')
        with mock.patch.object(teammate, 'interpret', side_effect=teammate.TeammateError('down')) as interpret:
            codes = [self.client.post(f'/api/runs/{other}/interpret').status_code for _ in range(server.MAX_INTERPRET + 1)]
        self.assertEqual(codes, [503] * server.MAX_INTERPRET + [429])
        self.assertEqual(interpret.call_count, server.MAX_INTERPRET)

    def test_judge_code_selects_the_judge_pool(self):
        code = 'correct-horse-battery-staple-1234'
        digest = hashlib.sha256(code.encode()).hexdigest()
        answer = {'text': 'Why?', 'usage': {}, 'seconds': 0.1}
        with mock.patch.dict(os.environ, {'JUDGE_CODE_SHA256': digest}):
            self.assertTrue(self.client.get('/api/judge', headers={'X-Judge-Code': code}).json()['judge'])
            self.assertFalse(self.client.get('/api/judge', headers={'X-Judge-Code': 'wrong'}).json()['judge'])
            rid = self.start()['id']
            with mock.patch.object(teammate, 'reply', return_value=answer) as reply:
                self.client.post(f'/api/runs/{rid}/chat', json={'text': 'hi'}, headers={'X-Judge-Code': code})
                self.client.post(f'/api/runs/{rid}/chat', json={'text': 'hi'})
        pools = [call.args[3].pool for call in reply.call_args_list]
        self.assertEqual(pools, ['judge', 'public'])
        with mock.patch.dict(os.environ, {'JUDGE_CODE_SHA256': ''}):
            self.assertFalse(self.client.get('/api/judge', headers={'X-Judge-Code': code}).json()['judge'])

    def test_fly_client_ip_is_trusted_only_when_enabled(self):
        def visitor(headers, trust):
            with mock.patch.dict(os.environ, {'TRUST_FLY_CLIENT_IP': '1' if trust else ''}):
                request = mock.Mock(headers=headers, client=mock.Mock(host='10.0.0.1'))
                return server._visitor(request)
        self.assertEqual(visitor({'fly-client-ip': '1.2.3.4'}, False), visitor({}, False))
        self.assertNotEqual(visitor({'fly-client-ip': '1.2.3.4'}, True), visitor({'fly-client-ip': '5.6.7.8'}, True))

    def test_new_shifts_are_rate_limited_per_visitor(self):
        server._new_runs.clear()
        old = server.RUNS_PER_HOUR
        server.RUNS_PER_HOUR = 2
        try:
            codes = [self.client.post('/api/runs', json={'variant': 'A'}).status_code for _ in range(3)]
        finally:
            server.RUNS_PER_HOUR = old
            server._new_runs.clear()
        self.assertEqual(codes, [200, 200, 429])

    def test_plan_stress_test_flow(self):
        rid = self.start()['id']
        rollback = plans.validate({'steps': [{'act': 'rollback'}, {'wait': True}], 'unsupported': ['Call my manager']})
        self.assertEqual(self.client.post(f'/api/runs/{rid}/plans', json={'text': 'roll back'}).status_code, 400)
        self.client.post(f'/api/runs/{rid}/resolve')
        with mock.patch.object(teammate, 'compile_plan', return_value=rollback) as compile_plan:
            body = self.client.post(f'/api/runs/{rid}/plans', json={'text': '  If checkout fails,  roll back. '}).json()
        self.assertEqual(compile_plan.call_args.args[0], 'If checkout fails, roll back.')
        plan = body['plans'][0]
        self.assertIsNone(plan['results'])
        self.assertEqual(plan['unsupported'], ['Call my manager'])
        self.assertEqual(body['plans_left'], server.MAX_PLANS - 1)
        ran = self.client.post(f'/api/runs/{rid}/plans/1/run').json()['plans'][0]
        self.assertEqual(plans.verdict(ran['results']['B']['stats']), 'broke checkout')
        explanation = {'points': [{'kind': 'change', 'text': 'Check the migration first.', 'facts': ['B-F2']}], 'dropped': 0}
        with mock.patch.object(teammate, 'explain_plan', return_value=explanation) as explain:
            first = self.client.post(f'/api/runs/{rid}/plans/1/explain').json()
            self.client.post(f'/api/runs/{rid}/plans/1/explain')
        explain.assert_called_once()
        self.assertEqual(first['plans'][0]['explanation'], explanation)

        better = plans.validate({'steps': [{'look': 'release', 'arg': 'v2.14.0'},
                                           {'if': 'migration_irreversible', 'then': [{'act': 'flag', 'arg': 'tax_v2_quotes', 'to': 'off'}],
                                            'else': [{'act': 'rollback'}]}]})
        with mock.patch.object(teammate, 'compile_plan', return_value=better):
            self.client.post(f'/api/runs/{rid}/plans', json={'text': 'check first'})
        second = self.client.post(f'/api/runs/{rid}/plans/2/run').json()['plans'][1]
        self.assertEqual(second['versus']['previous'], 1)
        self.assertEqual(second['versus']['B'][0]['second'], 'recovered and stayed healthy')
        self.assertEqual(self.client.post(f'/api/runs/{rid}/plans/9/run').status_code, 404)

    def test_plan_compile_failure_and_cap(self):
        rid = self.start()['id']
        self.client.post(f'/api/runs/{rid}/resolve')
        with mock.patch.object(teammate, 'compile_plan', side_effect=teammate.TeammateError('nope')):
            self.assertEqual(self.client.post(f'/api/runs/{rid}/plans', json={'text': 'x'}).status_code, 503)
        plan = plans.validate({'steps': [{'wait': True}]})
        with mock.patch.object(teammate, 'compile_plan', return_value=plan):
            codes = [self.client.post(f'/api/runs/{rid}/plans', json={'text': 'x'}).status_code for _ in range(server.MAX_PLANS + 1)]
        self.assertEqual(codes, [200] * server.MAX_PLANS + [429])

    def test_replay_comparison_rows_have_ids(self):
        rid = self.start()['id']
        self.client.post(f'/api/runs/{rid}/resolve')
        bid = self.start('B', rid)['id']
        final = self.client.post(f'/api/runs/{bid}/resolve').json()
        self.assertEqual([r['id'] for r in final['comparison']], [f'C{i}' for i in range(1, 7)])


if __name__ == '__main__':
    unittest.main(verbosity=2)
