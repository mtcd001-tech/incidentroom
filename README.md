# Incident Room

**Practice your first production outage before you're responsible for a real one.**

You're on call for checkout at a fictional shop. Checkout is failing for about one in five customers. You investigate with metrics, logs, deploy history, feature flags and a runbook, choose a fix, and watch the simulated service respond. Then **Pressure Replay** runs the same outage with one thing changed: the release shipped an irreversible database migration, so rolling back now breaks every checkout.

## How it works

- **A fixed simulation decides every outcome.** Service state (release, flags, timeouts, pods, payment route) drives each minute's metrics and logs. The same choices always produce the same result, so recovery is something you can demonstrate, not something a model declares.
- **Time costs customers.** Every look takes a simulated minute; changes take minutes to roll out; failed checkouts keep counting.
- **The replay lives in the scenario.** In the replay, the release notes, database logs and the rollback's actual effect all change. It is not a prompt instruction.
- **Evidence-linked debrief.** Every point in the debrief cites the timeline events it is based on (for example `E4`), and the replay is compared with the original run using recorded stats only.

## Status

| Part | State |
|---|---|
| Simulation, both scenario variants, timeline, debrief facts, replay comparison | Working; covered by unit and API tests |
| Nemotron teammate (discusses hypotheses, asks for evidence) | Not built yet |
| Nemotron interpretation layer for the debrief (cites the facts, kept separate from them) | Not built yet |
| Hosted demo | Not deployed yet |

## Run it locally

Requires Python 3.11+.

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python server.py
```

Open http://127.0.0.1:8780. The simulation needs no API key.

## Tests

```powershell
.venv\Scripts\python -m unittest test_sim test_server
```

## Project layout

| Path | What it is |
|---|---|
| `scenario.py` | The authored incident: timings, releases, flags, runbook, both variants |
| `sim.py` | Deterministic simulation: state, metrics, logs, actions, timeline |
| `facts.py` | Debrief facts with event citations, and the run-vs-replay comparison |
| `server.py` | FastAPI app and JSON API |
| `static/` | The web interface |

Everything in the scenario (company, services, people, orders) is fictional.

## License

MIT
