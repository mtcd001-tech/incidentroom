import hashlib
import hmac
import os
import secrets
import threading
import time
from collections import deque
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import budget
import facts
import scenario as S
import sim
import teammate

ROOT = Path(__file__).parent
load_dotenv(ROOT / '.env')
HOST = os.getenv('INCIDENT_HOST', '127.0.0.1')
PORT = int(os.getenv('INCIDENT_PORT', '8780'))
MAX_RUNS = 500
RUN_TTL = 3 * 60 * 60
MAX_CHAT = 12   # trainee messages per run; each one is a paid model call
MAX_INTERPRET = 2   # debrief interpretation attempts per run (the second only when the trainee clicks "Try again")
RUNS_PER_HOUR = 30  # new runs per visitor per hour

runs: dict[str, dict] = {}
_new_runs: dict[str, deque] = {}
_new_runs_lock = threading.Lock()

app = FastAPI(title='Incident Room', docs_url=None, redoc_url=None)
app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')


class NewRun(BaseModel):
    variant: Literal['A', 'B'] = 'A'
    previous: str | None = Field(default=None, max_length=64)


class Look(BaseModel):
    tool: Literal['metrics', 'logs', 'deploys', 'release', 'flags', 'runbook']
    arg: str | None = Field(default=None, max_length=40)


class Act(BaseModel):
    action: str = Field(max_length=40)
    arg: str | None = Field(default=None, max_length=40)
    text: str = Field(default='', max_length=600)


class Chat(BaseModel):
    text: str = Field(min_length=1, max_length=500)


def _visitor(request: Request) -> str:
    """A salted hash of the client IP. Fly-Client-IP is trusted only when the app runs behind Fly's proxy."""
    ip = request.headers.get('fly-client-ip') if os.getenv('TRUST_FLY_CLIENT_IP') == '1' else None
    ip = ip or (request.client.host if request.client else 'unknown')
    return hashlib.sha256((os.getenv('VISITOR_SALT', '') + ip).encode()).hexdigest()[:20]


def _is_judge(request: Request) -> bool:
    code, expected = request.headers.get('x-judge-code', ''), os.getenv('JUDGE_CODE_SHA256', '').strip().lower()
    return bool(code and expected) and hmac.compare_digest(hashlib.sha256(code.encode()).hexdigest(), expected)


def _spend(request: Request) -> budget.Spend:
    return budget.Spend('judge' if _is_judge(request) else 'public', _visitor(request))


def _allow_new_run(visitor: str):
    now = time.time()
    with _new_runs_lock:
        recent = _new_runs.setdefault(visitor, deque())
        while recent and recent[0] < now - 3600:
            recent.popleft()
        if len(recent) >= RUNS_PER_HOUR:
            raise HTTPException(429, 'Too many new shifts from your connection. Please wait a little.')
        recent.append(now)
        if len(_new_runs) > 5000:
            _new_runs.clear()


def _prune():
    cutoff = time.time() - RUN_TTL
    for run_id in [k for k, v in runs.items() if v['touched'] < cutoff]:
        del runs[run_id]
    while len(runs) >= MAX_RUNS:
        del runs[min(runs, key=lambda k: runs[k]['touched'])]


def _get(run_id: str) -> dict:
    run = runs.get(run_id)
    if not run:
        raise HTTPException(404, 'This run has expired. Start a new shift.')
    run['touched'] = time.time()
    return run


def _payload(run: dict, result: dict | None = None) -> dict:
    inc = run['incident']
    asked = sum(1 for m in run['chat'] if m['role'] == 'trainee')
    body = {'id': run['id'], 'view': inc.view(), 'result': result, 'previous': run['previous'],
            'chat': run['chat'], 'chat_left': max(0, MAX_CHAT - asked)}
    if inc.ended:
        body['debrief'], comparison = _debrief(run)
        if comparison:
            body['comparison'] = comparison
        body['interpretation'] = run['interpretation']
        body['interpret_left'] = MAX_INTERPRET - run['interpret_tries']
        body['interpret_tried'] = run['interpret_tries'] > 0   # the page asks automatically only before the first try
    return body


def _debrief(run: dict) -> tuple[dict, list | None]:
    summary = facts.summarize(run['incident'])
    previous = runs.get(run['previous']) if run['previous'] else None
    if previous and previous['incident'].ended:
        return summary, facts.compare(facts.summarize(previous['incident']), summary)
    return summary, None


def _command(run_id: str, fn):
    run = _get(run_id)
    try:
        result = fn(run['incident'])
    except sim.CommandError as exc:
        raise HTTPException(400, str(exc))
    return _payload(run, result)


@app.get('/')
def index():
    return FileResponse(ROOT / 'static' / 'index.html')


@app.get('/api/scenario')
def scenario_info():
    return {'variants': S.VARIANT_INFO}


@app.get('/api/judge')
def judge(request: Request):
    return {'judge': _is_judge(request)}


@app.post('/api/runs')
def new_run(body: NewRun, request: Request):
    _allow_new_run(_visitor(request))
    _prune()
    previous = body.previous if body.previous in runs else None
    run = {'id': secrets.token_urlsafe(16), 'incident': sim.Incident(body.variant), 'previous': previous, 'chat': [],
           'interpretation': None, 'interpret_tries': 0, 'touched': time.time()}
    runs[run['id']] = run
    return _payload(run)


@app.get('/api/runs/{run_id}')
def get_run(run_id: str):
    return _payload(_get(run_id))


@app.post('/api/runs/{run_id}/look')
def look(run_id: str, body: Look):
    return _command(run_id, lambda inc: inc.look(body.tool, body.arg))


@app.post('/api/runs/{run_id}/act')
def act(run_id: str, body: Act):
    return _command(run_id, lambda inc: inc.act(body.action, body.arg, body.text))


@app.post('/api/runs/{run_id}/wait')
def wait(run_id: str):
    return _command(run_id, lambda inc: inc.wait())


@app.post('/api/runs/{run_id}/resolve')
def resolve(run_id: str):
    return _command(run_id, lambda inc: inc.resolve())


@app.post('/api/runs/{run_id}/chat')
def chat(run_id: str, body: Chat, request: Request):
    """Ask the Nemotron teammate. Talking costs no simulated time and is not written to the timeline."""
    run = _get(run_id)
    inc = run['incident']
    text = ' '.join(body.text.split())
    if not text:
        raise HTTPException(400, 'Type a message first.')
    if inc.ended:
        raise HTTPException(400, 'This shift has ended.')
    if sum(1 for m in run['chat'] if m['role'] == 'trainee') >= MAX_CHAT:
        raise HTTPException(429, 'You have used all teammate messages for this run.')
    try:
        answer = teammate.reply(inc, run['chat'], text, _spend(request))
    except teammate.TeammateError as exc:
        raise HTTPException(503, str(exc))
    run['chat'] += [{'role': 'trainee', 'text': text, 'clock': S.clock(inc.now)},
                    {'role': 'teammate', 'text': answer['text'], 'clock': S.clock(inc.now)}]
    return _payload(run)


@app.post('/api/runs/{run_id}/interpret')
def interpret(run_id: str, request: Request):
    """Nemotron's reading of the debrief. Generated once per run; every point is checked against the cited facts."""
    run = _get(run_id)
    if not run['incident'].ended:
        raise HTTPException(400, 'The debrief is ready when the shift ends.')
    if run['interpretation']:
        return _payload(run)
    if run['interpret_tries'] >= MAX_INTERPRET:
        raise HTTPException(429, 'No more AI debrief attempts for this run.')
    run['interpret_tries'] += 1
    summary, comparison = _debrief(run)
    try:
        run['interpretation'] = teammate.interpret(run['incident'], summary, comparison, _spend(request))
    except teammate.TeammateError as exc:
        raise HTTPException(503, str(exc))
    return _payload(run)


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)
