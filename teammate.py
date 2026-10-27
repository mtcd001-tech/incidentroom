"""Nemotron teammate. It coaches with questions and only sees what the trainee has seen.

The simulation decides every outcome; this module never changes the incident. One model call per message, no retries.
"""
import os
import time

import httpx

import scenario as S

DEFAULT_BASE_URL = 'https://api.tokenfactory.us-north1.nebius.com/v1/'
DEFAULT_MODEL = 'nvidia/Nemotron-3.5-Super-VL'
MAX_TOKENS = 220
TIMEOUT = 20
HISTORY = 8          # chat messages sent back to the model
MAX_REPLY_CHARS = 900

RULES = """You are a senior engineer on the checkout team at Fernway Outdoor, sitting next to a junior engineer who is on call for the first time. This is a training simulation; the company and the incident are fictional.

How you help:
- Coach, don't solve. Do not name the root cause and do not tell them which fix to apply. Ask one short question that makes them look at evidence or explain their reasoning.
- You only know the situation below: what the trainee has looked at and done so far. Never invent metrics, logs, releases or facts. If they ask about something that is not there, ask what they would check to find out.
- If they propose an action, ask what evidence supports it and what could go wrong, especially for changes that are slow or hard to undo.
- If they state a hypothesis that fits evidence they have seen, you may say it fits that evidence, then ask what they will check or do next.
- Respond to their latest message and the newest evidence. Do not repeat a question you already asked.
- Never say whether an action will work. The simulation decides, and they will see it in the metrics.
- Reply in 1 to 3 short sentences of plain English. No lists, no markdown."""


class TeammateError(RuntimeError):
    pass


def situation(inc) -> str:
    """Everything the teammate may know: the briefing, the runbook, the status bar and the trainee's own timeline."""
    v = inc.view()
    info = S.VARIANT_INFO[inc.variant]
    lines = [f"Scenario: {info['title']}. Briefing the trainee received: {info['briefing']}", '',
             'Team runbook (the trainee can open it at any time):']
    for title, steps in S.RUNBOOK:
        lines.append(f'{title}: ' + ' '.join(steps))
    lines += ['', f"Now {v['clock']}, {v['elapsed']} of {v['limit']} minutes into the shift. Status: {v['status']}. "
              f"Checkout 5xx error rate {v['error_rate']}%, p99 latency {v['p99_ms']} ms, "
              f"failed checkouts this shift {v['failed']}."]
    if v['pending']:
        lines.append('Still rolling out: ' + '; '.join(f"{p['label']} (about {p['minutes_left']} min left)" for p in v['pending']))
    lines += ['', "The trainee's timeline so far (nothing else is known):"]
    if not v['events']:
        lines.append('Nothing yet: they have not looked at anything or acted.')
    for e in v['events']:
        lines.append(f"{e['id']} {e['clock']} {e['label']}" + (f": {e['detail']}" if e['detail'] else '') + _seen(inc, e))
    return '\n'.join(lines)


def _seen(inc, event: dict) -> str:
    """Extra content the trainee saw on screen for looks whose timeline detail is short."""
    if event['kind'] != 'look':
        return ''
    if event['name'] == 'deploys':
        return ' | ' + '; '.join(f"{r['version']} ({r['service']}, {r['when']}): {r['title']}" for r in S.releases(inc.variant))
    if event['name'] == 'release':
        match = [r for r in S.releases(inc.variant) if r['version'] == event['arg']]
        return (' | Changes: ' + '; '.join(match[0]['changes'])) if match else ''
    return ''


def messages_for(inc, chat: list, text: str) -> list:
    history = [{'role': 'user' if m['role'] == 'trainee' else 'assistant', 'content': m['text']} for m in chat[-HISTORY:]]
    return [{'role': 'system', 'content': f'{RULES}\n\n{situation(inc)}'}, *history, {'role': 'user', 'content': text}]


def reply(inc, chat: list, text: str) -> dict:
    key = os.getenv('NEBIUS_API_KEY', '').strip()
    if not key:
        raise TeammateError('The AI teammate is not configured on this server.')
    base = os.getenv('NEBIUS_BASE_URL', DEFAULT_BASE_URL).rstrip('/') + '/'
    started = time.perf_counter()
    try:
        res = httpx.post(base + 'chat/completions', headers={'Authorization': f'Bearer {key}'}, timeout=TIMEOUT, json={
            'model': os.getenv('NEBIUS_MODEL', DEFAULT_MODEL),
            'messages': messages_for(inc, chat, text),
            'max_tokens': MAX_TOKENS,
            'temperature': 0.4,
            'chat_template_kwargs': {'enable_thinking': False},
        })
    except httpx.HTTPError:
        raise TeammateError('The AI teammate did not answer. The simulation keeps running; try again later.')
    if res.status_code != 200:
        raise TeammateError(f'The AI teammate is unavailable right now (HTTP {res.status_code}).')
    try:
        data = res.json()
        content = data['choices'][0]['message']['content'] or ''
    except (ValueError, KeyError, IndexError, TypeError):
        raise TeammateError('The AI teammate sent an unreadable answer.')
    content = content.split('</think>')[-1].strip()[:MAX_REPLY_CHARS]
    if not content:
        raise TeammateError('The AI teammate sent an empty answer.')
    return {'text': content, 'usage': data.get('usage') or {}, 'seconds': round(time.perf_counter() - started, 2)}
