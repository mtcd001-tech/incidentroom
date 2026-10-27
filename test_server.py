import unittest

from fastapi.testclient import TestClient

import server


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


if __name__ == '__main__':
    unittest.main(verbosity=2)
