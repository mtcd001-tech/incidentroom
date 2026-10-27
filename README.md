# Incident Room

**Practice your first production outage before you're responsible for a real one.**

**Live demo:** https://incident-room-demo.fly.dev

You're on call for checkout at a fictional shop. Checkout is failing for about one in five customers. You investigate with metrics, logs, deploy history, feature flags and a runbook, choose a fix, and watch the simulated service respond. Then **Pressure Replay** runs the same outage with one thing changed: the release shipped an irreversible database migration, so rolling back now breaks every checkout.

## How it works

- **A fixed simulation decides every outcome.** Service state (release, flags, timeouts, pods, payment route) drives each minute's metrics and logs. The same choices always produce the same result, so recovery is something you can demonstrate, not something a model declares.
- **Time costs customers.** Every look takes a simulated minute; changes take minutes to roll out; failed checkouts keep counting.
- **The replay lives in the scenario.** In the replay, the release notes, database logs and the rollback's actual effect all change. It is not a prompt instruction.
- **A Nemotron teammate coaches, it doesn't solve.** You can talk the incident through with an AI teammate (NVIDIA Nemotron via Nebius Token Factory). It only sees what you have seen: the briefing, the runbook, the status bar and your own timeline, never the hidden cause. It answers with questions about evidence and risk, and it never decides whether an action works. Talking costs no simulated time and is not written to the timeline.
- **Evidence-linked debrief.** Every point in the debrief cites the timeline events it is based on (for example `E4`), and the replay is compared with the original run using recorded stats only.
- **Stress-test your plan.** After the debrief, write what you would do next time in plain English ("if checkout fails after a deploy, roll back"). Nemotron translates it into simulator steps, keeping your plan as written, even its mistakes, and lists anything it had to assume or could not translate. You approve the steps; the simulator runs that exact plan on fresh copies of both versions of the outage (migration reversible / irreversible) over the same 20 minutes and shows the results side by side. Revise the plan and compare it with the previous one. "Your plan worked once. Here is the condition that breaks it."
- **The AI interpretation is checked, and kept apart from the facts.** After the shift, Nemotron writes a short "Coach's read" (what to keep doing, what to change next time). Every point must cite fact IDs (`F2`, or `C1` for comparison rows). The server drops any point that cites an unknown ID or uses a number or time that is not in the cited facts, their timeline events or the runbook. It is shown in its own panel, labelled as AI.

## How Nemotron and Nebius Token Factory are used

Every model call goes to **NVIDIA Nemotron 3.5 Super** (`nvidia/Nemotron-3.5-Super-VL`) through **Nebius Token Factory**'s OpenAI-compatible `chat/completions` endpoint. The model has four jobs, and none of them decides an outcome:

| Job | When | Calls | What keeps it honest |
|---|---|---|---|
| **Teammate** | During the shift | 1 per message (`max_tokens` 220, at most 12 per run) | Sees only the briefing, runbook, status bar and the trainee's own timeline, never the hidden cause. Must answer with a question, not a list of steps. |
| **Coach's read** | After the shift | 1 per run (`max_tokens` 450) | Each point must cite fact IDs; points that cite unknown IDs, use numbers not in the cited facts, or have the wrong tone (a "keep" point needs a good fact, a "change" point a gap) are dropped. |
| **Plan translator** | Stress test | 1 per plan (`max_tokens` 600, at most 4 per run) | Must keep the trainee's plan as written. Output is strict JSON that is validated before anything runs (only simulator steps, one migration if/else, no nesting, ≤ 8 steps per path). Assumptions and unsupported parts are shown to the trainee, who approves the steps. |
| **Plan explainer** | After a plan runs | 1 per plan (`max_tokens` 450) | Same citation and number checks, with facts scoped per version (`A-F2`, `B-F3`). |

- **Thinking is turned off** (`chat_template_kwargs: {"enable_thinking": false}`). In our first test with thinking on, 589 of 600 output tokens went to hidden reasoning and the reply was cut off after one sentence (2.15 s). With thinking off, the same request answered in 0.54 s with 23 tokens.
- **No retries, 20-second timeout.** A failed call shows a short message; the simulation keeps working without the AI.
- **Every call is paid through a spending guard** (see [Spending limits](#spending-limits)).

## Status

| Part | State |
|---|---|
| Simulation, both scenario variants, timeline, debrief facts, replay comparison | Working; covered by unit and API tests |
| Nemotron teammate (discusses hypotheses, asks for evidence) | Working; mocked tests, and checked with live Nemotron calls locally and on the hosted demo |
| Nemotron Coach's read for the debrief (cites the facts, kept separate from them) | Working; mocked tests, and checked with live Nemotron calls locally and on the hosted demo |
| Plan Stress Test (Nemotron translates and explains; the simulator runs the plan) | Working; unit and API tests with a mocked model, and checked live locally and on the hosted demo (including a phone-width layout) |
| Spending guard ($5 total, daily share, judge pool, visitor limits) | Working; unit tests, and checked on the hosted demo (charge recorded, record survives a restart) |
| Hosted demo | Live at https://incident-room-demo.fly.dev (Fly.io, one machine) |

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

Then create the spending record once (it is never created automatically, so a lost record turns the AI off instead of resetting the budget):

```powershell
.venv\Scripts\python budget.py init --spent-before 0.02   # USD already spent on development calls
.venv\Scripts\python budget.py report                     # spending so far
```

Optional overrides: `NEBIUS_MODEL` (default `nvidia/Nemotron-3.5-Super-VL`) and `NEBIUS_BASE_URL` (default `https://api.tokenfactory.us-north1.nebius.com/v1/`). Without a key or a spending record the simulation works normally and the teammate panel shows that the AI is unavailable.

### Spending limits

Every model call first reserves its worst-case cost (prompt bytes as an upper bound on tokens, plus `max_tokens`) in a SQLite record, then settles at the real cost from the returned usage; if the outcome is uncertain, the reservation stays counted. Settings (environment variables, defaults in brackets):

| Variable | Meaning |
|---|---|
| `BUDGET_DB` [`data/budget.db`] | The spending record. On a host, put it on a persistent volume. |
| `BUDGET_TOTAL_USD` [5] | Hard total, including `--spent-before` |
| `BUDGET_JUDGE_USD` [1] | Reserved for judges (used with a valid judge code) |
| `BUDGET_END_DATE` [2026-12-15] | The public share is spread evenly per UTC day until this date |
| `VISITOR_DAILY_CALLS` [25], `JUDGE_DAILY_CALLS` [200] | Model calls per visitor per day, failed calls included |
| `JUDGE_CODE_SHA256` | SHA-256 hex of the judge code; judges enter the code on the start page (sent as a header) |
| `TRUST_FLY_CLIENT_IP` | Set to `1` only behind Fly.io's proxy, to identify visitors by `Fly-Client-IP` |
| `VISITOR_SALT` | Secret salt for the hashed visitor IDs |

Each visitor can run one model call at a time and start 30 shifts per hour.

The Coach's read is saved once it succeeds; if it fails, the trainee can press "Try again" once. Each plan's explanation is tried once.

## Deploy (Fly.io)

One always-on machine (shared-cpu-1x, 256 MB) with a 1 GB volume for the spending record, about $2.35 a month. The Docker image contains only the app code, never `.env`.

```powershell
fly auth login
fly apps create <your-app-name>          # then put the same name in fly.toml → app
fly volumes create budget_data --size 1 --region iad -a <your-app-name>
fly secrets set NEBIUS_API_KEY=<key> VISITOR_SALT=<random text> JUDGE_CODE_SHA256=<sha256 of the judge code> -a <your-app-name>
fly deploy --ha=false                    # one machine only (plain `fly deploy` adds a spare)
fly ssh console -a <your-app-name> -C "python /app/budget.py init --spent-before <USD spent so far>"
fly ssh console -a <your-app-name> -C "python /app/budget.py report"
```

Until the spending record is created, the site works and the AI features say they are paused. Keep exactly one machine (`fly scale count 1`): visitor limits and the one-call-at-a-time rule live in that process.

## Tests

```powershell
.venv\Scripts\python -m unittest test_sim test_server test_teammate test_budget test_plans
```

The tests replace the model with a fake, so they never make a paid call.

## Project layout

| Path | What it is |
|---|---|
| `scenario.py` | The authored incident: timings, releases, flags, runbook, both variants |
| `sim.py` | Deterministic simulation: state, metrics, logs, actions, timeline |
| `facts.py` | Debrief facts with event citations, and the run-vs-replay comparison |
| `teammate.py` | Nemotron teammate and debrief read: what the model may see, its rules, the Token Factory call, and the citation check |
| `plans.py` | Recovery plans: strict validation of the model's JSON, deterministic execution on both variants, comparisons |
| `budget.py` | Spending guard: reserve before each model call, settle after, daily/total/judge caps |
| `server.py` | FastAPI app and JSON API |
| `static/` | The web interface |

Everything in the scenario (company, services, people, orders) is fictional.

## License

MIT
