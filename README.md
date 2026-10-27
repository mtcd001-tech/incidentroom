# Incident Room

**Practice your first production outage before you're responsible for a real one.**

You're on call for checkout at a fictional shop. Checkout is failing for about one in five customers. You investigate with metrics, logs, deploy history, feature flags and a runbook, choose a fix, and watch the simulated service respond. Then **Pressure Replay** runs the same outage with one thing changed: the release shipped an irreversible database migration, so rolling back now breaks every checkout.

## How it works

- **A fixed simulation decides every outcome.** Service state (release, flags, timeouts, pods, payment route) drives each minute's metrics and logs. The same choices always produce the same result, so recovery is something you can demonstrate, not something a model declares.
- **Time costs customers.** Every look takes a simulated minute; changes take minutes to roll out; failed checkouts keep counting.
- **The replay lives in the scenario.** In the replay, the release notes, database logs and the rollback's actual effect all change. It is not a prompt instruction.
- **A Nemotron teammate coaches, it doesn't solve.** You can talk the incident through with an AI teammate (NVIDIA Nemotron via Nebius Token Factory). It only sees what you have seen: the briefing, the runbook, the status bar and your own timeline, never the hidden cause. It answers with questions about evidence and risk, and it never decides whether an action works. Talking costs no simulated time and is not written to the timeline.
- **Evidence-linked debrief.** Every point in the debrief cites the timeline events it is based on (for example `E4`), and the replay is compared with the original run using recorded stats only.
- **The AI interpretation is checked, and kept apart from the facts.** After the shift, Nemotron writes a short "Coach's read" (what to keep doing, what to change next time). Every point must cite fact IDs (`F2`, or `C1` for comparison rows). The server drops any point that cites an unknown ID or uses a number or time that is not in the cited facts, their timeline events or the runbook. It is shown in its own panel, labelled as AI.

## Status

| Part | State |
|---|---|
| Simulation, both scenario variants, timeline, debrief facts, replay comparison | Working; covered by unit and API tests |
| Nemotron teammate (discusses hypotheses, asks for evidence) | Working; mocked tests, and checked with live Nemotron calls in a local browser |
| Nemotron interpretation layer for the debrief (cites the facts, kept separate from them) | Working; mocked tests, and checked with live Nemotron calls in a local browser |
| Hosted demo | Not deployed yet |

## Run it locally

Requires Python 3.11+.

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python server.py
```

Open http://127.0.0.1:8780. The simulation needs no API key.

### Nemotron teammate (optional)

Create a `.env` file next to `server.py`:

```
NEBIUS_API_KEY=your-nebius-token-factory-key
```

Optional overrides: `NEBIUS_MODEL` (default `nvidia/Nemotron-3.5-Super-VL`) and `NEBIUS_BASE_URL` (default `https://api.tokenfactory.us-north1.nebius.com/v1/`). Without a key the simulation works normally and the teammate panel shows that it is unavailable.

How the call is made: one OpenAI-compatible `chat/completions` request per message, with Nemotron's thinking turned off (`chat_template_kwargs: {"enable_thinking": false}`) for fast, short replies, `max_tokens` 220, a 20-second timeout and no retries. Each run allows 12 teammate messages. The debrief read is one call per run (450 `max_tokens`), saved once it succeeds; if it fails, the trainee can press "Try again" once.

## Tests

```powershell
.venv\Scripts\python -m unittest test_sim test_server test_teammate
```

The tests replace the model with a fake, so they never make a paid call.

## Project layout

| Path | What it is |
|---|---|
| `scenario.py` | The authored incident: timings, releases, flags, runbook, both variants |
| `sim.py` | Deterministic simulation: state, metrics, logs, actions, timeline |
| `facts.py` | Debrief facts with event citations, and the run-vs-replay comparison |
| `teammate.py` | Nemotron teammate and debrief read: what the model may see, its rules, the Token Factory call, and the citation check |
| `server.py` | FastAPI app and JSON API |
| `static/` | The web interface |

Everything in the scenario (company, services, people, orders) is fictional.

## License

MIT
