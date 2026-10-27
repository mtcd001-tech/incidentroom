import unittest
from unittest import mock

from fastapi.testclient import TestClient

import server
import teammate


class ServerTests(unittest.TestCase):
    def setUp(self):
        server.runs.clear()
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


if __name__ == '__main__':
    unittest.main(verbosity=2)
