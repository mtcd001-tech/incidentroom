"""Nemotron teammate. It coaches with questions and only sees what the trainee has seen.

After the shift it also writes the debrief interpretation, which must cite the simulation's facts.
The simulation decides every outcome; this module never changes the incident. One model call per request, no retries.
"""
import json
import os
import re
import time

import httpx

import budget
import plans
import scenario as S

DEFAULT_BASE_URL = 'https://api.tokenfactory.us-north1.nebius.com/v1/'
DEFAULT_MODEL = 'nvidia/Nemotron-3.5-Super-VL'
MAX_TOKENS = 220
TIMEOUT = 20
HISTORY = 8          # chat messages sent back to the model
MAX_REPLY_CHARS = 900
INTERPRET_TOKENS = 450
COMPILE_TOKENS = 600
MAX_POINTS = 3

RULES = """You are a senior engineer on the checkout team at Fernway Outdoor, sitting next to a junior engineer who is on call for the first time. This is a training simulation; the company and the incident are fictional.

How you help:
- Coach, don't solve. Do not name the root cause and do not tell them which fix to apply. Ask one short question that makes them look at evidence or explain their reasoning.
- Never give instructions or a list of steps, even when they ask "where should I look?" or "what should I do?". Turn it back into a question about what they want to find out. Every reply ends with exactly one question.
  Bad: "Check the checkout metrics, then look at recent deploys."
  Good: "What would tell you whether this started with a change on our side? Where could you see what changed recently?"
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
- A "keep" point must cite at least one fact tagged [good]; a "change" point must cite at least one fact tagged [gap].
- At least one point should be something to change next time, unless the facts show nothing went wrong.
Return only JSON, no other text: {"points": [{"kind": "keep" or "change", "text": "...", "facts": ["F1"]}]}"""


class TeammateError(RuntimeError):
    pass


def model_name() -> str:
    return os.getenv('NEBIUS_MODEL', DEFAULT_MODEL)


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


def _complete(messages: list, max_tokens: int, temperature: float, spend) -> dict:
    """One chat/completions call with thinking off, paid for through `spend` (a budget.Spend).

    The worst-case cost is reserved before the request and settled from the returned usage afterwards.
    Raises TeammateError on any failure; never retries."""
    key = os.getenv('NEBIUS_API_KEY', '').strip()
    if not key:
        raise TeammateError('The AI teammate is not configured on this server.')
    base = os.getenv('NEBIUS_BASE_URL', DEFAULT_BASE_URL).rstrip('/') + '/'
    model = model_name()
    try:
        ticket = spend.reserve(model, budget.max_cost(model, messages, max_tokens))
    except budget.BudgetError as exc:
        raise TeammateError(str(exc))
    usage = None
    started = time.perf_counter()
    try:
        try:
            res = httpx.post(base + 'chat/completions', headers={'Authorization': f'Bearer {key}'}, timeout=TIMEOUT, json={
                'model': model,
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
            usage = data.get('usage') if isinstance(data.get('usage'), dict) else None
            content = data['choices'][0]['message']['content'] or ''
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise TeammateError('The AI teammate sent an unreadable answer.')
    finally:
        spend.finish(ticket, model, usage)
    content = content.split('</think>')[-1].strip()
    if not content:
        raise TeammateError('The AI teammate sent an empty answer.')
    return {'text': content, 'usage': usage or {}, 'seconds': round(time.perf_counter() - started, 2)}


def _tidy(text: str) -> str:
    """Drop a sentence the model repeated back to back (seen live: "…failing?What do you…failing?")."""
    # A sentence ends at . ! ? followed by space or a capital letter, never inside 21.7% or v2.14.0.
    sentences = [s for s in re.split(r'(?<=[.!?])(?:\s+|(?=[A-Z]))', text.strip()) if s]
    kept = [s for i, s in enumerate(sentences) if i == 0 or s != sentences[i - 1]]
    return ' '.join(kept)


def reply(inc, chat: list, text: str, spend) -> dict:
    answer = _complete(messages_for(inc, chat, text), MAX_TOKENS, 0.4, spend)
    return dict(answer, text=_tidy(answer['text'])[:MAX_REPLY_CHARS])


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


_REF = re.compile(r'\(?\b(?:[AB]-)?[EFC]\d+\b\)?')
_NUMBER = re.compile(r'\d+(?:[.,:]\d+)*')


def _numbers(text: str) -> set:
    return {n.replace(',', '') for n in _NUMBER.findall(_REF.sub(' ', text))}


def check_points(raw: str, sources: dict, common: str = '', tones: dict | None = None) -> tuple[list, int]:
    """Keep only points that cite known sources and use no number missing from them (or from `common`, the runbook).

    With `tones` (fact ID -> good/gap/info), a "keep" point must cite a good fact and a "change" point a gap fact.
    This is a partial filter: it cannot tell whether the cited facts really support the sentence. Returns (points, dropped)."""
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
            kind = point.get('kind') if isinstance(point.get('kind'), str) else None
            text = re.sub(r'\s+([.,;:!?])', r'\1', ' '.join(_REF.sub(' ', str(point['text'])).split()))
            refs = list(dict.fromkeys(str(r).strip().upper() for r in point['facts']))
        except (AttributeError, KeyError, TypeError):
            dropped += 1
            continue
        cited = ' '.join(sources.get(r, '') for r in refs) + ' ' + common
        needed = {'keep': 'good', 'change': 'gap'}.get(kind)
        if (not needed or not text or not refs or any(r not in sources for r in refs)
                or not _numbers(text) <= _numbers(cited)
                or (tones is not None and not any(tones.get(r) == needed for r in refs))):
            dropped += 1
            continue
        kept.append({'kind': kind, 'text': text[:400], 'facts': refs})
    return kept[:MAX_POINTS], dropped + max(0, len(kept) - MAX_POINTS)


def _fact_sources(fact_list: list, events: list) -> dict:
    """A fact's evidence is its own text plus the timeline events it cites."""
    by_id = {e['id']: e for e in events}
    return {f['id']: ' '.join([f['text'], *(f"{by_id[r]['clock']} {by_id[r]['label']} {by_id[r]['detail']}"
                                            for r in f['events'] if r in by_id)]) for f in fact_list}


def _checked(rules: str, context: str, sources: dict, tones: dict, spend) -> dict:
    answer = _complete([{'role': 'system', 'content': rules}, {'role': 'user', 'content': context}], INTERPRET_TOKENS, 0.3, spend)
    runbook = ' '.join(' '.join(steps) for _, steps in S.RUNBOOK)
    points, dropped = check_points(answer['text'], sources, runbook, tones)
    if not points:
        raise TeammateError('The AI explanation could not be checked against the facts, so it is not shown.')
    return {'points': points, 'dropped': dropped}


def interpret(inc, summary: dict, comparison: list | None, spend) -> dict:
    sources = _fact_sources(summary['facts'], inc.events)
    sources.update({r['id']: f"{r['label']}: {r['first']}, {r['second']}" for r in comparison or []})
    tones = {f['id']: f['tone'] for f in summary['facts']}
    return _checked(INTERPRET_RULES, debrief_context(inc, summary, comparison), sources, tones, spend)


# ---- plan stress test -----------------------------------------------------------------------------------------
def _vocabulary() -> str:
    looks = []
    for tool, args in plans.LOOKS.items():
        names = [a for a in args if a]
        looks.append(f'{{"look": "{tool}"' + (f', "arg": one of {names}' if names else '') + '}')
    acts = [f'{{"act": "{name}"}} = {plans.describe({"act": name, "arg": None})}' for name in plans.ACTS if name != 'flag']
    flags = f'{{"act": "flag", "arg": one of {list(S.FLAGS)}, "to": "on" or "off"}}'
    return '\n'.join(['Looks:', *looks, 'Actions:', *acts, flags, 'Wait:',
                      f'{{"wait": true}} = watch the dashboards for {plans.sim.WAIT_MINUTES} minutes'])


COMPILE_RULES = """You turn a junior on-call engineer's recovery plan, written in plain English, into steps for a training simulator. You are a translator, not an advisor.

The incident: checkout failures started right after checkout-api {new} was deployed at 14:02 (the previous version is {old}).

Rules:
- Translate their plan exactly as written, including mistakes and unsafe steps. Never add, remove, reorder or improve steps.
- Every step must come from words in their plan. Do not add a status update, a look or a wait they did not ask for. Something the simulator cannot do goes in "unsupported", never as a different step.
- Use only the steps listed below.
- Only if their plan says to decide based on whether the release's database migration can be undone, use one block: {{"if": "migration_irreversible", "then": [steps], "else": [steps]}}. "then" runs when the migration is irreversible. No other conditions, no nesting, at most one block. The simulator can only decide this after the plan has opened the {new} release notes, so do not add that look yourself; if their plan decides without reading the notes, keep it that way.
- At most {max_steps} steps on any path.
- "assumptions": short notes wherever you had to interpret vague wording.
- "unsupported": parts of their plan that the simulator cannot do (for example "call my manager").

Allowed steps:
{vocabulary}

Return only JSON, no other text: {{"steps": [...], "assumptions": [...], "unsupported": [...]}}"""


def compile_plan(text: str, spend) -> dict:
    rules = COMPILE_RULES.format(new=S.NEW, old=S.OLD, max_steps=plans.MAX_STEPS, vocabulary=_vocabulary())
    answer = _complete([{'role': 'system', 'content': rules}, {'role': 'user', 'content': text}], COMPILE_TOKENS, 0.1, spend)
    raw = answer['text']
    start, end = raw.find('{'), raw.rfind('}')
    try:
        parsed = json.loads(raw[start:end + 1]) if start != -1 else None
        return plans.drop_unrequested_status(plans.validate(parsed), text)
    except (ValueError, TypeError, AttributeError) as exc:   # JSONDecodeError and PlanError are ValueErrors
        reason = str(exc) if isinstance(exc, plans.PlanError) else 'the AI answer was not a valid plan'
        raise TeammateError(f'Could not turn that into simulator steps: {reason} Try rewording your plan.')


EXPLAIN_RULES = """You are a senior engineer explaining to a junior engineer how their recovery plan did when a training simulator ran it, unchanged, on two versions of the same outage: A, where the release's database migration is reversible, and B, where it is irreversible so rolling back breaks checkout. The facts were computed by the simulator and are correct.

Rules:
- Write 2 or 3 points. Each point is one or two sentences of plain English.
- Every point must list the IDs it is based on: A-F1, B-F2 and so on, or C1, C2 for comparison rows.
- A "keep" point must cite at least one fact tagged [good]; a "change" point must cite at least one fact tagged [gap].
- Use only numbers and times that appear in the facts you cite, the events they are based on, or the comparison rows.
- Explain which part of the plan caused the difference between A and B, and what to change in the plan. Do not write a new plan for them.
Return only JSON, no other text: {"points": [{"kind": "keep" or "change", "text": "...", "facts": ["B-F2"]}]}"""


def explain_plan(plan: dict, results: dict, spend) -> dict:
    lines = ['The plan as run:'] + [f"{line['n'] or ''} {line['text']}".strip() for line in plans.outline(plan)]
    sources, tones = {}, {}
    for variant in 'AB':
        result = results[variant]
        lines += ['', f'Version {variant} ({"reversible" if variant == "A" else "irreversible"} migration):']
        if result['stopped']:
            lines.append(f"The plan stopped early: {result['stopped']}")
        lines += [f"{t['n']} {t['text']}" + (f" ({t['note']})" if t['note'] else '') for t in result['trace']]
        lines += [f"{f['id']} [{f['tone']}] {f['text']}" for f in result['facts']]
        sources.update(_fact_sources(result['facts'], result['events']))
        tones.update({f['id']: f['tone'] for f in result['facts']})
    lines += ['', 'Comparison (first = A, second = B):']
    lines += [f"{r['id']} {r['label']}: A {r['first']}, B {r['second']}" for r in results['comparison']]
    sources.update({r['id']: f"{r['label']}: {r['first']}, {r['second']}" for r in results['comparison']})
    return _checked(EXPLAIN_RULES, '\n'.join(lines), sources, tones, spend)

