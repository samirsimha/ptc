import json

from fastapi import FastAPI, HTTPException
from falkordb import FalkorDB
from fastapi.middleware.cors import CORSMiddleware
from openai import OpenAIError
from pydantic import BaseModel
import schema, seed
import agents

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

db = FalkorDB(host="localhost", port=6379)
g = db.select_graph("conf")


class IncidentRequest(BaseModel):
    text: str


# send a Cypher query, get rows back
def run_cypher(q, params=None):
    return g.query(q, params or {}).result_set

# test endpoint: return every node
@app.get("/graph")
def get_graph():
    nodes = run_cypher("MATCH (n) RETURN id(n), labels(n)[0], properties(n)")
    edges = run_cypher("MATCH (a)-[r]->(b) RETURN id(a), id(b), type(r)")
    return {
        "nodes": [{"id": n[0], "label": n[1], "props": n[2]} for n in nodes],
        "links": [{"source": e[0], "target": e[1], "type": e[2]} for e in edges],
    }

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
def _get_proposed_fix(fix_id):
    rows = run_cypher(
        "MATCH (f:Fix {id: $id}) "
        "RETURN f.status, f.staged_changes, f.staged_session_changes",
        {"id": fix_id},
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Fix not found")
    if rows[0][0] != "proposed":
        raise HTTPException(status_code=409, detail="Fix is not awaiting a decision")

    try:
        talk_changes = json.loads(rows[0][1])
        session_changes = json.loads(rows[0][2] or "[]")
    except (TypeError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=409, detail="Fix has invalid staged changes") from error

    if not isinstance(talk_changes, list) or not isinstance(session_changes, list):
        raise HTTPException(status_code=409, detail="Fix has invalid staged changes")
    return talk_changes, session_changes


# Validate the stored schedule changes before changing any Talk or Session nodes.
def _validate_staged_changes(talk_changes, session_changes):
    for change in talk_changes:
        if (
            not isinstance(change, dict)
            or not isinstance(change.get("talk_id"), str)
            or not isinstance(change.get("start"), int)
            or isinstance(change.get("start"), bool)
            or not isinstance(change.get("end"), int)
            or isinstance(change.get("end"), bool)
            or change["start"] >= change["end"]
        ):
            raise HTTPException(status_code=409, detail="Fix contains invalid talk changes")
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
        if not run_cypher(
            "MATCH (s:Session {id: $id}) RETURN s.id",
            {"id": change["session_id"]},
        ):
            raise HTTPException(status_code=409, detail="Fix references a missing session")

    talks, sessions, presenters, attendees = agents._schedule_snapshot(g)
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
    talk_changes, session_changes = _get_proposed_fix(fix_id)
    _validate_staged_changes(talk_changes, session_changes)

    for change in talk_changes:
        run_cypher(
            "MATCH (t:Talk {id: $id}) "
            "SET t.start = $start, t.end = $end",
            {
                "id": change["talk_id"],
                "start": change["start"],
                "end": change["end"],
            },
        )
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
    run_cypher(
        "MATCH (f:Fix {id: $id}) SET f.status = 'applied'",
        {"id": fix_id},
    )
    return {"id": fix_id, "status": "applied"}


@app.post("/fixes/{fix_id}/reject")
def reject_fix(fix_id: str):
    _get_proposed_fix(fix_id)
    run_cypher(
        "MATCH (f:Fix {id: $id}) SET f.status = 'rejected'",
        {"id": fix_id},
    )
    return {"id": fix_id, "status": "rejected"}


@app.get("/fixes/{fix_id}/explain")
def explain_fix(fix_id: str):
    rows = run_cypher(
        "MATCH (agent:Agent)-[:PROPOSED]->(f:Fix {id: $id})"
        "-[:RESOLVES]->(incident:Incident)-[:AFFECTS]->(root:Talk) "
        "RETURN agent.name, f.id, f.proposal, f.status, incident.id, "
        "incident.type, incident.desc, root.id, root.title, f.staged_changes",
        {"id": fix_id},
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Fix explanation path not found")

    first = rows[0]
    try:
        staged_changes = json.loads(first[9] or "[]")
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
    }