"""Nemotron teammate. It coaches with questions and only sees what the trainee has seen.

After the shift it also writes the debrief interpretation, which must cite the simulation's facts.
The simulation decides every outcome; this module never changes the incident. One model call per request, no retries.
"""
import json
import os
import re
import time

import httpx

import scenario as S

DEFAULT_BASE_URL = 'https://api.tokenfactory.us-north1.nebius.com/v1/'
DEFAULT_MODEL = 'nvidia/Nemotron-3.5-Super-VL'
MAX_TOKENS = 220
TIMEOUT = 20
HISTORY = 8          # chat messages sent back to the model
MAX_REPLY_CHARS = 900
INTERPRET_TOKENS = 450
MAX_POINTS = 3

RULES = """You are a senior engineer on the checkout team at Fernway Outdoor, sitting next to a junior engineer who is on call for the first time. This is a training simulation; the company and the incident are fictional.

How you help:
- Coach, don't solve. Do not name the root cause and do not tell them which fix to apply. Ask one short question that makes them look at evidence or explain their reasoning.
- You only know the situation below: what the trainee has looked at and done so far. Never invent metrics, logs, releases or facts. If they ask about something that is not there, ask what they would check to find out.
- If they propose an action, ask what evidence supports it and what could go wrong, especially for changes that are slow or hard to undo.
- If they state a hypothesis that fits evidence they have seen, you may say it fits that evidence, then ask what they will check or do next.
- Respond to their latest message and the newest evidence. Do not repeat a question you already asked.
- Never say whether an action will work. The simulation decides, and they will see it in the metrics.
- Reply in 1 to 3 short sentences of plain English. No lists, no markdown."""


INTERPRET_RULES = """You are a senior engineer giving a short, kind and honest debrief to a junior engineer after a simulated on-call shift. The facts below were computed by the simulation and are correct. Your job is interpretation: what the trainee should keep doing and what to change next time, judged against the team runbook.

Rules:
- Write 2 or 3 points. Each point is one or two sentences of plain English.
- Every point must list the IDs of the facts it is based on: F1, F2 and so on, or C1, C2 for comparison rows.
- Use only numbers and times that appear in the facts you cite, the timeline events those facts are based on, or the runbook. Do not invent events, numbers or causes.
- Do not write fact or event IDs inside the text; put them in "facts".
- At least one point should be something to change next time, unless the facts show nothing went wrong.
Return only JSON, no other text: {"points": [{"kind": "keep" or "change", "text": "...", "facts": ["F1"]}]}"""


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


def _complete(messages: list, max_tokens: int, temperature: float) -> dict:
    """One chat/completions call with thinking off. Raises TeammateError on any failure; never retries."""
    key = os.getenv('NEBIUS_API_KEY', '').strip()
    if not key:
        raise TeammateError('The AI teammate is not configured on this server.')
    base = os.getenv('NEBIUS_BASE_URL', DEFAULT_BASE_URL).rstrip('/') + '/'
    started = time.perf_counter()
    try:
        res = httpx.post(base + 'chat/completions', headers={'Authorization': f'Bearer {key}'}, timeout=TIMEOUT, json={
            'model': os.getenv('NEBIUS_MODEL', DEFAULT_MODEL),
            'messages': messages,
            'max_tokens': max_tokens,
            'temperature': temperature,
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
    content = content.split('</think>')[-1].strip()
    if not content:
        raise TeammateError('The AI teammate sent an empty answer.')
    return {'text': content, 'usage': data.get('usage') or {}, 'seconds': round(time.perf_counter() - started, 2)}


def reply(inc, chat: list, text: str) -> dict:
    answer = _complete(messages_for(inc, chat, text), MAX_TOKENS, 0.4)
    return dict(answer, text=answer['text'][:MAX_REPLY_CHARS])


# ---- debrief interpretation ---------------------------------------------------------------------------------
def debrief_context(inc, summary: dict, comparison: list | None) -> str:
    lines = ['Team runbook:'] + [f'{title}: ' + ' '.join(steps) for title, steps in S.RUNBOOK]
    lines += ['', f"Scenario: {S.VARIANT_INFO[inc.variant]['title']}.", '', 'Timeline:']
    lines += [f"{e['id']} {e['clock']} {e['label']}" + (f": {e['detail']}" if e['detail'] else '') for e in inc.events]
    lines += ['', 'Facts (computed by the simulation), with the timeline events each one is based on:']
    lines += [f"{f['id']} [{f['tone']}] {f['text']}" + (f" (events: {', '.join(f['events'])})" if f['events'] else '')
              for f in summary['facts']]
    if comparison:
        lines += ['', 'Comparison with the original run (first = original run, second = this replay):']
        lines += [f"{r['id']} {r['label']}: first {r['first']}, second {r['second']}" for r in comparison]
    return '\n'.join(lines)


_REF = re.compile(r'\(?\b[EFC]\d+\b\)?')
_NUMBER = re.compile(r'\d+(?:[.,:]\d+)*')


def _numbers(text: str) -> set:
    return {n.replace(',', '') for n in _NUMBER.findall(_REF.sub(' ', text))}


def check_points(raw: str, sources: dict, common: str = '') -> tuple[list, int]:
    """Keep only points that cite known sources and use no number missing from them (or from `common`, the runbook).

    Returns (points, dropped)."""
    start, end = raw.find('{'), raw.rfind('}')
    try:
        points = json.loads(raw[start:end + 1])['points'] if start != -1 else None
    except (ValueError, KeyError, TypeError):
        points = None
    if not isinstance(points, list):
        raise TeammateError('The AI debrief came back in the wrong format.')
    kept, dropped = [], 0
    for point in points:
        try:
            kind = 'keep' if point.get('kind') == 'keep' else 'change'
            text = re.sub(r'\s+([.,;:!?])', r'\1', ' '.join(_REF.sub(' ', str(point['text'])).split()))
            refs = list(dict.fromkeys(str(r).strip().upper() for r in point['facts']))
        except (AttributeError, KeyError, TypeError):
            dropped += 1
            continue
        cited = ' '.join(sources.get(r, '') for r in refs) + ' ' + common
        if not text or not refs or any(r not in sources for r in refs) or not _numbers(text) <= _numbers(cited):
            dropped += 1
            continue
        kept.append({'kind': kind, 'text': text[:400], 'facts': refs})
    return kept[:MAX_POINTS], dropped + max(0, len(kept) - MAX_POINTS)


def interpret(inc, summary: dict, comparison: list | None = None) -> dict:
    messages = [{'role': 'system', 'content': INTERPRET_RULES},
                {'role': 'user', 'content': debrief_context(inc, summary, comparison)}]
    answer = _complete(messages, INTERPRET_TOKENS, 0.3)
    events = {e['id']: e for e in inc.events}
    # A fact's evidence is its own text plus the timeline events it cites.
    sources = {f['id']: ' '.join([f['text'], *(f"{events[r]['clock']} {events[r]['label']} {events[r]['detail']}"
                                               for r in f['events'] if r in events)]) for f in summary['facts']}
    sources.update({r['id']: f"{r['label']}: {r['first']}, {r['second']}" for r in comparison or []})
    runbook = ' '.join(' '.join(steps) for _, steps in S.RUNBOOK)
    points, dropped = check_points(answer['text'], sources, runbook)
    if not points:
        raise TeammateError('The AI debrief could not be checked against the facts, so it is not shown.')
    return {'points': points, 'dropped': dropped}
