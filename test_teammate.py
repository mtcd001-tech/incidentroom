"""Teammate tests. The model is always mocked here; no test makes a real Nebius call."""
import os
import unittest
from unittest import mock

import httpx

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


if __name__ == '__main__':
    unittest.main(verbosity=2)
