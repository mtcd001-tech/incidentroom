import os
import secrets
import time
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import facts
import scenario as S
import sim

ROOT = Path(__file__).parent
load_dotenv(ROOT / '.env')
HOST = os.getenv('INCIDENT_HOST', '127.0.0.1')
PORT = int(os.getenv('INCIDENT_PORT', '8780'))
MAX_RUNS = 500
RUN_TTL = 3 * 60 * 60

runs: dict[str, dict] = {}

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
    body = {'id': run['id'], 'view': inc.view(), 'result': result, 'previous': run['previous']}
    if inc.ended:
        summary = facts.summarize(inc)
        body['debrief'] = summary
        previous = runs.get(run['previous']) if run['previous'] else None
        if previous and previous['incident'].ended:
            body['comparison'] = facts.compare(facts.summarize(previous['incident']), summary)
    return body


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


@app.post('/api/runs')
def new_run(body: NewRun):
    _prune()
    previous = body.previous if body.previous in runs else None
    run = {'id': secrets.token_urlsafe(16), 'incident': sim.Incident(body.variant), 'previous': previous, 'touched': time.time()}
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


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)
