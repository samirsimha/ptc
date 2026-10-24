# Blast Radius

An explainable conference operations demo built on FalkorDB, FastAPI, and a single page graph UI. Incidents, cascades, proposed fixes, rule rejections, and notification drafts all live in one graph. Schedule changes require organizer approval; earlier moves also require presenter confirmation.

## Run locally

Requirements: Python 3.11+, Docker with Compose (Colima works on macOS). FalkorDB exposes `localhost:6379`. The optional FalkorDB browser is at `http://localhost:3000` when started with the `browser` profile.

```bash
docker-compose up -d
python3.11 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open `http://localhost:8000`. The graph is seeded automatically on first startup with a fixed seed. Reset from the UI or `POST /api/reset`. The bundled `vis-network` file makes graph viewing work without a frontend build or CDN; the optional Google font may fall back to system fonts while offline.

To launch FalkorDB's own browser as a fallback, run `docker-compose --profile browser up -d`.

On macOS, Colima can provide the Docker runtime. Run `colima start` before `docker-compose up -d` if it is stopped. The original `uvicorn main:app` entry point and `/graph` endpoint remain available.

If Docker is unavailable, Python 3.12+ can run the same FalkorDB graph embedded: `python3.14 -m venv .venv314`, `.venv314/bin/pip install -e '.[test,lite]'`, then `FALKORDB_LITE=1 .venv314/bin/uvicorn app.main:app --port 8000`. This persists to `.data/falkordb.db`. The app also falls back automatically when the server is unreachable and the lite extra is installed.

## Demo sequence

1. Enter **Keynote running 20 minutes over**. Follow `NEXT_IN` to three downstream talks and 31 attendees with projected RSVP clashes. The shift-only plan is rejected because it would leave those clashes unresolved. Approve the recovery plan: it shifts the talks and changes each conflicting live RSVP to a replay offer. The incident turns resolved in the graph.
2. Enter **Dr. Mira Solis didn't show**. See checked-in co-author Samir Rao and the topic path. Approve the substitute.
3. Enter **Power is out in the east wing**. Review powered, capacity-safe room moves, then approve.
4. Enter **Lab tour just opened to 300 RSVPs**. See capacity and catering gaps. Approve a deterministic 100 confirmed / 200 waitlisted plan.
5. Enter **Panel first speaker ran 25 minutes over** for time debt. Try **Move talk earlier by 5 minutes** to demonstrate mandatory presenter confirmation.
6. Click **Pre-flight scan** for audience overlap, chair conflict, walking time with a step-free alternative, and Q&A patterns.

The UI drafts notices for the presenters and attendees reached through the affected graph paths. It never sends them externally. A replay offer is tracked in demo graph state; no video delivery service is connected.

## API

| Route | Purpose |
|---|---|
| `GET /api/state` | Graph, incidents, fixes, drafts |
| `POST /api/incidents` | `{"text":"Keynote running 20 minutes over"}`; optional structured `details` |
| `POST /api/fixes/{id}/approve` | Revalidate then apply a proposal |
| `POST /api/fixes/{id}/reject` | Reject a proposal |
| `POST /api/fixes/{id}/confirm` | `{"person_id":"speaker-14"}` for earlier moves |
| `GET /api/preflight` | Before-conference checks |
| `POST /api/reset` | Restore deterministic seed |
| `GET /api/health` | Check FalkorDB connection |

Structured incident details use `type` plus appropriate `talk_id`, `event_id`, `session_id`, `wing`, and `minutes`. Supported types: `overrun`, `no_show`, `oversold`, `power`, `pull_forward`, `panel_debt`, `panel_cancelled`, `misassignment`, `qa_hog`.

Set `OPENAI_API_KEY` to enable OpenAI tool-based intent extraction. The deterministic parser handles the scripted scenarios without a network call or key. Neither path lets the model directly mutate the graph; rule checks and approvals run in code.

Set `OPENAI_AGENTS=1` to also run all five agent prompts with a constrained, read-only Cypher tool at each stage. Their observations are stored as `AgentRun` nodes linked to the shared incident. The default `0` keeps live incident tracing fast while preserving the five-stage graph workflow.

## Project layout

- `app/seed.py`: deterministic graph with planted scenarios, including six programmed sessions and one open overflow session for rescued talks
- `app/engine.py`: Triage, Schedule, Logistics, Comms, Coordinator
- `app/db.py`: parameterized FalkorDB reads and writes
- `app/main.py`: HTTP API
- `app/static/`: one page UI and bundled network visualizer
- `tests/`: integration tests against a local FalkorDB instance

Run `./.venv/bin/pytest -q` after starting FalkorDB. Tests use an isolated graph named `blast_radius_test` and remove it afterward.

The repository includes a pull-request CI workflow that starts FalkorDB and runs the same integration suite. A repository admin can enable Qodo's GitHub review integration for PR feedback.

## Notes

The reference overrun Cypher writes times immediately. This implementation calculates the shifted times and RSVP clashes without changing the published schedule, then writes them only when approved. That keeps the human approval rule meaningful. Notifications remain drafts for review; there is no external email or SMS transport configured.
