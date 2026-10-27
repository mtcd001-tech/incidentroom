"""Teammate tests. The model is always mocked here; no test makes a real Nebius call."""
import json
import os
import unittest
from unittest import mock

import httpx

import facts
import sim
import teammate


class FakeResponse:
    def __init__(self, status=200, content='What does the error rate tell you?'):
        self.status_code = status
        self.content = content

    def json(self):
        return {'choices': [{'message': {'content': self.content}}], 'usage': {'prompt_tokens': 900, 'completion_tokens': 20}}


def fake_post(response=None, error=None):
    def post(url, **kwargs):
        post.calls.append((url, kwargs))
        if error:
            raise error
        return response or FakeResponse()
    post.calls = []
    return post


@mock.patch.dict(os.environ, {'NEBIUS_API_KEY': 'test-key'})
class TeammateTests(unittest.TestCase):
    def test_situation_only_contains_what_the_trainee_has_seen(self):
        inc = sim.Incident('A')
        before = teammate.situation(inc)
        self.assertIn('Nothing yet', before)
        for hidden in ('tax_v2_quotes', '/v2/quote', '800 ms', 'tax_slow', 'Tax v2 quotes', 'load-tested'):
            self.assertNotIn(hidden, before)
        inc.look('deploys')
        inc.look('release', 'v2.14.0')
        inc.look('flags')
        after = teammate.situation(inc)
        self.assertIn('E1', after)
        self.assertIn('Tax v2 quotes for all regions', after)
        self.assertIn('Lower the tax client timeout from 3000 ms to 800 ms', after)
        self.assertIn('tax_v2_quotes ON', after)

    def test_replay_situation_shows_the_irreversible_migration_only_after_reading_it(self):
        inc = sim.Incident('B')
        self.assertNotIn('IRREVERSIBLE', teammate.situation(inc))
        inc.look('release', 'v2.14.0')
        self.assertIn('IRREVERSIBLE', teammate.situation(inc))

    def test_one_call_with_thinking_off_and_capped_tokens(self):
        inc = sim.Incident('A')
        chat = [{'role': 'trainee', 'text': 'Errors are up.'}, {'role': 'teammate', 'text': 'Since when?'}]
        post = fake_post()
        with mock.patch.object(teammate.httpx, 'post', post):
            answer = teammate.reply(inc, chat, 'Should I roll back?')
        self.assertEqual(answer['text'], 'What does the error rate tell you?')
        self.assertEqual(len(post.calls), 1)
        url, kwargs = post.calls[0]
        self.assertTrue(url.endswith('/v1/chat/completions'))
        body = kwargs['json']
        self.assertEqual(body['chat_template_kwargs'], {'enable_thinking': False})
        self.assertEqual(body['max_tokens'], teammate.MAX_TOKENS)
        self.assertEqual([m['role'] for m in body['messages']], ['system', 'user', 'assistant', 'user'])
        self.assertEqual(body['messages'][-1]['content'], 'Should I roll back?')

    def test_failures_raise_once_without_retrying(self):
        inc = sim.Incident('A')
        for post in (fake_post(error=httpx.ConnectTimeout('slow')), fake_post(FakeResponse(status=500)),
                     fake_post(FakeResponse(content='  '))):
            with mock.patch.object(teammate.httpx, 'post', post):
                with self.assertRaises(teammate.TeammateError):
                    teammate.reply(inc, [], 'Hello?')
            self.assertEqual(len(post.calls), 1)

    def test_missing_key_never_calls_the_model(self):
        post = fake_post()
        with mock.patch.dict(os.environ, {'NEBIUS_API_KEY': ''}), mock.patch.object(teammate.httpx, 'post', post):
            with self.assertRaises(teammate.TeammateError):
                teammate.reply(sim.Incident('A'), [], 'Hello?')
        self.assertEqual(post.calls, [])


SOURCES = {'F1': 'Checkout was healthy from 14:13, 7 minutes into your shift.',
           'F2': 'About 1,240 checkouts failed during your shift.',
           'C1': 'Time to healthy checkout: 7 min, 12 min'}


class InterpretationTests(unittest.TestCase):
    def check(self, points):
        return teammate.check_points(json.dumps({'points': points}), SOURCES)

    def test_points_must_cite_known_facts(self):
        kept, dropped = self.check([
            {'kind': 'keep', 'text': 'You got checkout healthy 7 minutes in.', 'facts': ['F1']},
            {'kind': 'change', 'text': 'Something about the database.', 'facts': ['F9']},
            {'kind': 'change', 'text': 'No citation here.', 'facts': []},
        ])
        self.assertEqual([p['facts'] for p in kept], [['F1']])
        self.assertEqual(dropped, 2)

    def test_numbers_must_come_from_the_cited_facts(self):
        kept, dropped = self.check([
            {'kind': 'change', 'text': 'About 1,240 checkouts failed (F2).', 'facts': ['f2']},
            {'kind': 'change', 'text': 'About 1,240 checkouts failed in 3 minutes.', 'facts': ['F2']},
            {'kind': 'change', 'text': 'It took 7 min here and 12 min in the replay.', 'facts': ['C1']},
            {'kind': 'keep', 'text': 'You recovered at 14:13.', 'facts': ['F2']},
        ])
        self.assertEqual([p['text'] for p in kept], ['About 1,240 checkouts failed.', 'It took 7 min here and 12 min in the replay.'])
        self.assertEqual(kept[0]['facts'], ['F2'])
        self.assertEqual(dropped, 2)

    def test_runbook_numbers_are_allowed(self):
        point = {'kind': 'change', 'text': 'Post a status update within 10 minutes.', 'facts': ['F2']}
        self.assertEqual(len(self.check([point])[0]), 0)
        kept, _ = teammate.check_points(json.dumps({'points': [point]}), SOURCES, 'Post an update within 10 minutes.')
        self.assertEqual(len(kept), 1)

    def test_invalid_kind_and_wrong_tone_are_dropped(self):
        tones = {'F1': 'good', 'F2': 'gap', 'C1': None}
        points = [{'kind': 'praise', 'text': 'Nice work.', 'facts': ['F1']},
                  {'kind': 'keep', 'text': 'About 1,240 checkouts failed.', 'facts': ['F2']},
                  {'kind': 'change', 'text': 'It took 7 min here and 12 min in the replay.', 'facts': ['C1']},
                  {'kind': 'keep', 'text': 'You got checkout healthy 7 minutes in.', 'facts': ['F1']},
                  {'kind': 'change', 'text': 'About 1,240 checkouts failed.', 'facts': ['F2', 'C1']},
                  {'kind': ['keep'], 'text': 'Odd kind.', 'facts': ['F1']},
                  {'kind': {'a': 1}, 'text': 'Odd kind.', 'facts': ['F1']}]
        kept, dropped = teammate.check_points(json.dumps({'points': points}), SOURCES, '', tones)
        self.assertEqual([(p['kind'], p['facts']) for p in kept], [('keep', ['F1']), ('change', ['F2', 'C1'])])
        self.assertEqual(dropped, 5)

    def test_bad_format_raises(self):
        for raw in ('Sure! Here is your debrief.', '{"points": "none"}', '{broken'):
            with self.assertRaises(teammate.TeammateError):
                teammate.check_points(raw, SOURCES)

    @mock.patch.dict(os.environ, {'NEBIUS_API_KEY': 'test-key'})
    def test_interpret_makes_one_call_and_checks_the_answer(self):
        inc = sim.Incident('A')
        inc.act('rollback')
        for _ in range(4):
            inc.wait()
        inc.resolve()
        summary = facts.summarize(inc)
        first = summary['facts'][0]
        acted = next(e for e in inc.events if e['id'] == first['events'][0])['clock']
        good = json.dumps({'points': [{'kind': 'change', 'text': 'Open the release notes before rolling back.',
                                       'facts': [summary['facts'][2]['id']]},
                                      {'kind': 'keep', 'text': f'You acted at {acted}.', 'facts': [first['id']]}]})
        post = fake_post(FakeResponse(content='```json\n' + good + '\n```'))
        with mock.patch.object(teammate.httpx, 'post', post):
            result = teammate.interpret(inc, summary)
        self.assertEqual(len(post.calls), 1)
        prompt = post.calls[0][1]['json']['messages'][1]['content']
        self.assertIn(f"F1 [{summary['facts'][0]['tone']}]", prompt)
        self.assertEqual(result['points'][0]['text'], 'Open the release notes before rolling back.')
        self.assertEqual(len(result['points']), 2)   # the time comes from an event the cited fact is based on
        bad = json.dumps({'points': [{'kind': 'change', 'text': 'You lost 99,999 orders.', 'facts': ['F1']}]})
        with mock.patch.object(teammate.httpx, 'post', fake_post(FakeResponse(content=bad))):
            with self.assertRaises(teammate.TeammateError):
                teammate.interpret(inc, summary)


if __name__ == '__main__':
    unittest.main(verbosity=2)
