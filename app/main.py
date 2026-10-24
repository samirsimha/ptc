from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from threading import RLock

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .db import Store
from .engine import Agents, Coordinator, preflight
from .seed import seed


ROOT = Path(__file__).resolve().parent
lock = RLock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.store = Store()
    seed(app.state.store)
    yield


app = FastAPI(title="Blast Radius", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


class IncidentRequest(BaseModel):
    text: str = Field(min_length=3, max_length=1000)
    details: dict | None = None


class ConfirmationRequest(BaseModel):
    person_id: str


@app.get("/")
def home():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/health")
def health():
    try:
        app.state.store.read("RETURN 1")
        return {"ok": True, "database": "FalkorDB", "backend": app.state.store.backend, "graph": app.state.store.graph_name}
    except Exception as exc:
        raise HTTPException(503, f"FalkorDB unavailable: {exc}") from exc


@app.get("/api/state")
def state():
    store = app.state.store
    return {"incidents": store.incidents(), "fixes": store.fixes(),
            "notifications": store.notifications(), "graph": store.graph_data()}


@app.get("/graph")
def legacy_graph():
    """Keep the original repo's graph endpoint available to existing clients."""
    store = app.state.store
    nodes = store.read("MATCH (n) RETURN id(n),labels(n),properties(n)")
    edges = store.read("MATCH (a)-[r]->(b) RETURN id(a),id(b),type(r)")
    return {"nodes": [dict(id=r[0], label=r[1][0], props=r[2]) for r in nodes],
            "links": [dict(source=r[0], target=r[1], type=r[2]) for r in edges]}


@app.post("/api/incidents")
def incident(request: IncidentRequest):
    with lock:
        try:
            return Agents(app.state.store).run(request.text, request.details)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc


@app.post("/api/fixes/{fix_id}/approve")
def approve(fix_id: str):
    with lock:
        try:
            return Coordinator(app.state.store).approve(fix_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc


@app.post("/api/fixes/{fix_id}/reject")
def reject(fix_id: str):
    with lock:
        fix = app.state.store.get_fix(fix_id)
        if not fix:
            raise HTTPException(404, "fix not found")
        if fix["status"] != "proposed":
            raise HTTPException(409, "only proposed fixes can be rejected")
        app.state.store.q("MATCH (f:Fix {id:$id}) SET f.status='rejected',f.rejected_rule='rejected by organizer'", id=fix_id)
        return app.state.store.get_fix(fix_id)


@app.post("/api/fixes/{fix_id}/confirm")
def confirm(fix_id: str, request: ConfirmationRequest):
    with lock:
        try:
            return Coordinator(app.state.store).confirm(fix_id, request.person_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc


@app.get("/api/preflight")
def scan():
    return preflight(app.state.store)


@app.post("/api/reset")
def reset():
    with lock:
        return seed(app.state.store, reset=True)
