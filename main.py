import json
from pathlib import Path
from threading import Lock

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from falkordb import FalkorDB
from fastapi.middleware.cors import CORSMiddleware
from openai import OpenAIError
from pydantic import BaseModel
import schema, seed
import agents

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
WEB_DIR = Path(__file__).parent / "web"
app.mount("/web", StaticFiles(directory=WEB_DIR), name="web")

db = FalkorDB(host="localhost", port=6379)
g = db.select_graph("conf")
_fix_decision_lock = Lock()


class IncidentRequest(BaseModel):
    text: str


# send a Cypher query, get rows back
def run_cypher(q, params=None):
    return g.query(q, params or {}).result_set


@app.get("/", include_in_schema=False)
def get_dashboard():
    return FileResponse(WEB_DIR / "index.html")


# test endpoint: return every node
@app.get("/graph")
def get_graph():
    nodes = run_cypher("MATCH (n) RETURN id(n), labels(n)[0], properties(n)")
    edges = run_cypher("MATCH (a)-[r]->(b) RETURN id(a), id(b), type(r)")
    return {
        "nodes": [{"id": n[0], "label": n[1], "props": n[2]} for n in nodes],
        "links": [{"source": e[0], "target": e[1], "type": e[2]} for e in edges],
    }


@app.get("/schedule")
def get_schedule():
    rows = run_cypher(
        "MATCH (room:Room)<-[:IN_ROOM]-(talk:Talk) "
        "OPTIONAL MATCH (speaker:Person)-[:PRESENTS]->(talk) "
        "OPTIONAL MATCH (talk)-[:IN_SESSION]->(session:Session) "
        "RETURN room.id, room.name, talk.id, talk.title, talk.start, talk.end, "
        "talk.status, speaker.id, speaker.name, session.id, session.title "
        "ORDER BY room.name, talk.start"
    )
    rooms = {}
    for row in rows:
        room = rooms.setdefault(row[0], {
            "id": row[0],
            "name": row[1],
            "talks": [],
        })
        room["talks"].append({
            "id": row[2],
            "title": row[3],
            "start": row[4],
            "end": row[5],
            "status": row[6],
            "speaker_id": row[7],
            "speaker": row[8],
            "session_id": row[9],
            "session": row[10],
        })
    return sorted(rooms.values(), key=lambda room: room["name"])


@app.get("/fixes")
def get_fixes():
    rows = run_cypher("MATCH (f:Fix) RETURN f.id, f.proposal, f.status, f.rejected_rule")
    fixes = []
    for row in rows:
        fixes.append({
            "id": row[0],
            "proposal": row[1],
            "status": row[2],
            "rejected_rule": row[3],
        })
    return fixes

@app.get("/notifications")
def get_notis():
    rows = run_cypher("MATCH (n:Notification)-[:TO]->(p:Person) RETURN n.id, n.body, n.status, p.name")
    notifications = []
    for row in rows:
        notifications.append({
            "id": row[0],
            "body": row[1],
            "status": row[2],
            "recipient": row[3],
        })
    return notifications

@app.post("/reset")
def reset():
    seed.seed(g)
    return schema.audit(g)


@app.post("/incident")
def create_incident(request: IncidentRequest):
    try:
        return agents.run_pipeline(g, request.text)
    except agents.ModelConfigurationError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except OpenAIError as error:
        raise HTTPException(
            status_code=502,
            detail="Triage model request failed; check API availability and configuration",
        ) from error


# Load a proposed Fix and decode its staged schedule changes.
def _get_proposed_fix(fix_id, allow_applying=False):
    rows = run_cypher(
        "MATCH (f:Fix {id: $id}) "
        "RETURN f.status, f.staged_changes, f.staged_session_changes, "
        "f.staged_presenter_change",
        {"id": fix_id},
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Fix not found")
    status = rows[0][0]
    if status != "proposed" and not (allow_applying and status == "applying"):
        raise HTTPException(status_code=409, detail="Fix is not awaiting a decision")

    try:
        talk_changes = json.loads(rows[0][1])
        session_changes = json.loads(rows[0][2] or "[]")
        presenter_change = json.loads(rows[0][3] or "null")
    except (TypeError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=409, detail="Fix has invalid staged changes") from error

    if (
        not isinstance(talk_changes, list)
        or not isinstance(session_changes, list)
        or (presenter_change is not None and not isinstance(presenter_change, dict))
    ):
        raise HTTPException(status_code=409, detail="Fix has invalid staged changes")
    return status, talk_changes, session_changes, presenter_change


# Validate the stored schedule changes before changing any Talk or Session nodes.
def _validate_staged_changes(talk_changes, session_changes, presenter_change, recovering=False):
    talk_ids = set()
    session_ids = set()
    for change in talk_changes:
        if (
            not isinstance(change, dict)
            or not isinstance(change.get("talk_id"), str)
            or not isinstance(change.get("start"), int)
            or isinstance(change.get("start"), bool)
            or not isinstance(change.get("end"), int)
            or isinstance(change.get("end"), bool)
            or change["start"] >= change["end"]
            or ("status" in change and change["status"] != "scheduled")
        ):
            raise HTTPException(status_code=409, detail="Fix contains invalid talk changes")
        if change["talk_id"] in talk_ids:
            raise HTTPException(status_code=409, detail="Fix contains duplicate talk changes")
        talk_ids.add(change["talk_id"])
        if not run_cypher(
            "MATCH (t:Talk {id: $id}) RETURN t.id",
            {"id": change["talk_id"]},
        ):
            raise HTTPException(status_code=409, detail="Fix references a missing talk")

    for change in session_changes:
        if (
            not isinstance(change, dict)
            or not isinstance(change.get("session_id"), str)
            or not isinstance(change.get("start"), int)
            or isinstance(change.get("start"), bool)
            or not isinstance(change.get("end"), int)
            or isinstance(change.get("end"), bool)
            or change["start"] >= change["end"]
        ):
            raise HTTPException(status_code=409, detail="Fix contains invalid session changes")
        if change["session_id"] in session_ids:
            raise HTTPException(status_code=409, detail="Fix contains duplicate session changes")
        session_ids.add(change["session_id"])
        if not run_cypher(
            "MATCH (s:Session {id: $id}) RETURN s.id",
            {"id": change["session_id"]},
        ):
            raise HTTPException(status_code=409, detail="Fix references a missing session")

    if presenter_change is not None:
        required_ids = ("talk_id", "from_person_id", "to_person_id")
        if any(not isinstance(presenter_change.get(key), str) for key in required_ids):
            raise HTTPException(status_code=409, detail="Fix contains an invalid presenter change")
        if presenter_change["from_person_id"] == presenter_change["to_person_id"]:
            raise HTTPException(status_code=409, detail="Fix has the same current and proposed presenter")
        if not run_cypher(
            "MATCH (t:Talk {id: $talk_id}) RETURN t.id",
            {"talk_id": presenter_change["talk_id"]},
        ):
            raise HTTPException(status_code=409, detail="Fix references a missing presenter talk")
        current_presenter = run_cypher(
            "MATCH (p:Person {id: $person_id})-[:PRESENTS]->"
            "(t:Talk {id: $talk_id}) RETURN p.id",
            {
                "person_id": presenter_change["from_person_id"],
                "talk_id": presenter_change["talk_id"],
            },
        )
        proposed_presenter = run_cypher(
            "MATCH (p:Person {id: $person_id})-[:PRESENTS]->"
            "(t:Talk {id: $talk_id}) RETURN p.id",
            {
                "person_id": presenter_change["to_person_id"],
                "talk_id": presenter_change["talk_id"],
            },
        )
        substitute = run_cypher(
            "MATCH (p:Person {id: $person_id}) "
            "RETURN p.id, p.checked_in",
            {"person_id": presenter_change["to_person_id"]},
        )
        if not current_presenter and not (recovering and proposed_presenter):
            raise HTTPException(status_code=409, detail="Fix's original presenter assignment changed")
        if not substitute or substitute[0][1] is not True:
            raise HTTPException(status_code=409, detail="Fix's substitute is missing or not checked in")

    talks, sessions, presenters, attendees = agents._schedule_snapshot(g)
    if presenter_change is not None:
        talk_id = presenter_change["talk_id"]
        substitute_id = presenter_change["to_person_id"]
        presenter_already_swapped = talk_id in presenters.get(substitute_id, set())
        if presenter_already_swapped and not recovering:
            raise HTTPException(
                status_code=409,
                detail="Fix's substitute is already assigned to the affected talk",
            )
        proposed_times = {
            change["talk_id"]: (change["start"], change["end"])
            for change in talk_changes
        }
        target = talks[talk_id]
        target_start, target_end = proposed_times.get(
            talk_id,
            (target["start"], target["end"]),
        )
        for other_talk_id in presenters.get(substitute_id, set()) - {talk_id}:
            other = talks[other_talk_id]
            other_start, other_end = proposed_times.get(
                other_talk_id,
                (other["start"], other["end"]),
            )
            if target_start < other_end and target_end > other_start:
                raise HTTPException(
                    status_code=409,
                    detail="Fix is no longer safe to apply: substitute has an overlapping talk",
                )

    conflicts = agents._schedule_conflicts(
        talks,
        sessions,
        presenters,
        attendees,
        talk_changes,
        session_changes,
    )
    if conflicts:
        raise HTTPException(
            status_code=409,
            detail=f"Fix is no longer safe to apply: {conflicts[0]['message']}",
        )


@app.post("/fixes/{fix_id}/approve")
def approve_fix(fix_id: str):
    # Serialize decisions within this API process so validation and application cannot
    # race with another approval/rejection request handled by the same worker.
    with _fix_decision_lock:
        status, talk_changes, session_changes, presenter_change = _get_proposed_fix(
            fix_id, allow_applying=True
        )
        recovering = status == "applying"
        _validate_staged_changes(
            talk_changes, session_changes, presenter_change, recovering=recovering
        )
        if not recovering:
            claimed = run_cypher(
                "MATCH (f:Fix {id: $id, status: 'proposed'}) "
                "SET f.status = 'applying' RETURN f.id",
                {"id": fix_id},
            )
            if not claimed:
                raise HTTPException(status_code=409, detail="Fix is not awaiting a decision")

        for change in talk_changes:
            params = {
                "id": change["talk_id"],
                "start": change["start"],
                "end": change["end"],
            }
            if "status" in change:
                params["status"] = change["status"]
                query = (
                    "MATCH (t:Talk {id: $id}) "
                    "SET t.start = $start, t.end = $end, t.status = $status"
                )
            else:
                query = (
                    "MATCH (t:Talk {id: $id}) "
                    "SET t.start = $start, t.end = $end"
                )
            run_cypher(query, params)
        for change in session_changes:
            run_cypher(
                "MATCH (s:Session {id: $id}) "
                "SET s.start = $start, s.end = $end",
                {
                    "id": change["session_id"],
                    "start": change["start"],
                    "end": change["end"],
                },
            )
        if presenter_change is not None:
            already_swapped = run_cypher(
                "MATCH (:Person {id: $person_id})-[:PRESENTS]->"
                "(:Talk {id: $talk_id}) RETURN count(*)",
                {
                    "person_id": presenter_change["to_person_id"],
                    "talk_id": presenter_change["talk_id"],
                },
            )[0][0] > 0
            if not already_swapped:
                run_cypher(
                    "MATCH (old:Person {id: $from_id})-[r:PRESENTS]->"
                    "(talk:Talk {id: $talk_id}) DELETE r",
                    {
                        "from_id": presenter_change["from_person_id"],
                        "talk_id": presenter_change["talk_id"],
                    },
                )
                run_cypher(
                    "MATCH (new:Person {id: $to_id}), (talk:Talk {id: $talk_id}) "
                    "CREATE (new)-[:PRESENTS]->(talk)",
                    {
                        "to_id": presenter_change["to_person_id"],
                        "talk_id": presenter_change["talk_id"],
                    },
                )
        run_cypher(
            "MATCH (f:Fix {id: $id, status: 'applying'}) SET f.status = 'applied'",
            {"id": fix_id},
        )
        run_cypher(
            "MATCH (f:Fix {id: $id})-[:HAS_NOTIFICATION]->(n:Notification) "
            "WHERE n.status = 'draft' SET n.status = 'approved'",
            {"id": fix_id},
        )
    return {"id": fix_id, "status": "applied"}


@app.post("/fixes/{fix_id}/reject")
def reject_fix(fix_id: str):
    with _fix_decision_lock:
        _get_proposed_fix(fix_id)
        rejected = run_cypher(
            "MATCH (f:Fix {id: $id, status: 'proposed'}) "
            "SET f.status = 'rejected' RETURN f.id",
            {"id": fix_id},
        )
        if not rejected:
            raise HTTPException(status_code=409, detail="Fix is not awaiting a decision")
        run_cypher(
            "MATCH (f:Fix {id: $id})-[:HAS_NOTIFICATION]->(n:Notification) "
            "WHERE n.status = 'draft' SET n.status = 'cancelled'",
            {"id": fix_id},
        )
    return {"id": fix_id, "status": "rejected"}


@app.get("/fixes/{fix_id}/explain")
def explain_fix(fix_id: str):
    rows = run_cypher(
        "MATCH (agent:Agent)-[:PROPOSED]->(f:Fix {id: $id})"
        "-[:RESOLVES]->(incident:Incident)-[:AFFECTS]->(root:Talk) "
        "RETURN agent.name, f.id, f.proposal, f.status, incident.id, "
        "incident.type, incident.desc, root.id, root.title, f.staged_changes, "
        "f.staged_presenter_change",
        {"id": fix_id},
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Fix explanation path not found")

    first = rows[0]
    try:
        staged_changes = json.loads(first[9] or "[]")
        presenter_change = json.loads(first[10] or "null")
    except (TypeError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=409, detail="Fix has invalid staged changes") from error
    proposed_talks = []
    for change in staged_changes:
        talk_rows = run_cypher(
            "MATCH (t:Talk {id: $id}) RETURN t.id, t.title",
            {"id": change["talk_id"]},
        )
        if talk_rows:
            proposed_talks.append({"id": talk_rows[0][0], "title": talk_rows[0][1]})

    return {
        "agent": first[0],
        "fix": {
            "id": first[1],
            "proposal": first[2],
            "status": first[3],
        },
        "incident": {
            "id": first[4],
            "type": first[5],
            "description": first[6],
        },
        "affected_talk": {"id": first[7], "title": first[8]},
        "proposed_talks": proposed_talks,
        "presenter_change": presenter_change,
    }
