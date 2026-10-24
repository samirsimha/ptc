from fastapi import FastAPI
from falkordb import FalkorDB
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

db = FalkorDB(host="localhost", port=6379)
g = db.select_graph("conf")

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