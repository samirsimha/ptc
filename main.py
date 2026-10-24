from fastapi import FastAPI
from falkordb import FalkorDB
from fastapi.middleware.cors import CORSMiddleware
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
    return agents.record_incident(g, request.text)