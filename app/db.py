"""Small FalkorDB boundary. All incident state is persisted in one graph."""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from falkordb import FalkorDB


def uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, graph_name: str | None = None):
        self.backend = "server"
        if os.getenv("FALKORDB_LITE") == "1":
            self.db = self._lite()
        else:
            try:
                self.db = FalkorDB(
                    host=os.getenv("FALKORDB_HOST", "localhost"),
                    port=int(os.getenv("FALKORDB_PORT", "6379")),
                )
            except Exception:
                self.db = self._lite()
        self.graph_name = graph_name or os.getenv("FALKORDB_GRAPH", "blast_radius")
        self.graph = self.db.select_graph(self.graph_name)

    def _lite(self):
        try:
            from redislite.falkordb_client import FalkorDB as EmbeddedFalkorDB
        except ImportError as exc:
            raise RuntimeError("FalkorDB server is unavailable; install the lite extra with Python 3.12+ or start Docker") from exc
        path = Path(os.getenv("FALKORDB_LITE_PATH", Path(__file__).resolve().parent.parent / ".data" / "falkordb.db"))
        path.parent.mkdir(parents=True, exist_ok=True)
        self.backend = "embedded"
        return EmbeddedFalkorDB(str(path))

    def q(self, statement: str, **params):
        return self.graph.query(statement, params=params or None).result_set

    def read(self, statement: str, **params):
        return self.graph.ro_query(statement, params=params or None).result_set

    def reset(self):
        if self.graph_name in self.db.list_graphs():
            self.graph.delete()
        self.graph = self.db.select_graph(self.graph_name)

    def incident(self, kind: str, desc: str, source: str = "user", parent: str | None = None,
                 status: str = "active"):
        ident = uid("inc")
        self.q(
            "CREATE (i:Incident {id:$id, type:$kind, desc:$desc, ts:$ts, source:$source, status:$status})",
            id=ident, kind=kind, desc=desc, ts=now(), source=source, status=status,
        )
        if parent:
            self.q("MATCH (p:Incident {id:$parent}), (c:Incident {id:$child}) CREATE (p)-[:CAUSED]->(c)", parent=parent, child=ident)
        return ident

    def affects(self, incident_id: str, label: str, target_id: str):
        if label not in {"Talk", "Room", "Person", "Session", "SideEvent", "Catering"}:
            raise ValueError(label)
        self.q(f"MATCH (i:Incident {{id:$incident}}), (n:{label} {{id:$target}}) MERGE (i)-[:AFFECTS]->(n)", incident=incident_id, target=target_id)

    def fix(self, incident_id: str, agent: str, proposal: str, action: dict, path: list[str],
            rejected_rule: str | None = None, requires_confirmation: bool = False):
        ident = uid("fix")
        status = "rejected" if rejected_rule else "proposed"
        self.q("""
            MATCH (i:Incident {id:$incident}), (a:Agent {name:$agent})
            CREATE (f:Fix {id:$id, proposal:$proposal, status:$status,
              rejected_rule:$rule, action:$action, path:$path,
              requires_confirmation:$confirm, created:$created})
            CREATE (f)-[:RESOLVES]->(i), (a)-[:PROPOSED]->(f)
        """, incident=incident_id, agent=agent, id=ident, proposal=proposal,
             status=status, rule=rejected_rule or "", action=json.dumps(action),
             path=json.dumps(path), confirm=requires_confirmation, created=now())
        return ident

    def notification(self, incident_id: str, fix_id: str, person_id: str, subject: str, body: str):
        ident = uid("note")
        self.q("""
            MATCH (i:Incident {id:$incident}), (f:Fix {id:$fix}), (p:Person {id:$person})
            CREATE (n:Notification {id:$id, incident_id:$incident, fix_id:$fix,
              person_id:$person, subject:$subject, body:$body, status:'draft'})
            CREATE (i)-[:DRAFTED]->(n), (n)-[:TO]->(p), (f)-[:HAS_DRAFT]->(n)
        """, incident=incident_id, fix=fix_id, person=person_id,
             id=ident, subject=subject, body=body)
        return ident

    def notifications_bulk(self, incident_id: str, fix_id: str, drafts: list[dict]):
        if not drafts:
            return
        rows = [dict(id=uid("note"), person_id=d["person_id"], subject=d["subject"], body=d["body"]) for d in drafts]
        self.q("""
            MATCH (i:Incident {id:$incident}), (f:Fix {id:$fix})
            UNWIND $rows AS row
            MATCH (p:Person {id:row.person_id})
            CREATE (n:Notification {id:row.id, incident_id:$incident, fix_id:$fix,
              person_id:row.person_id, subject:row.subject, body:row.body, status:'draft'})
            CREATE (i)-[:DRAFTED]->(n), (n)-[:TO]->(p), (f)-[:HAS_DRAFT]->(n)
        """, incident=incident_id, fix=fix_id, rows=rows)

    def get_fix(self, ident: str):
        rows = self.read("MATCH (f:Fix {id:$id})-[:RESOLVES]->(i:Incident) RETURN f.id,f.proposal,f.status,f.rejected_rule,f.action,f.path,f.requires_confirmation,i.id,i.type", id=ident)
        if not rows:
            return None
        r = rows[0]
        return dict(id=r[0], proposal=r[1], status=r[2], rejected_rule=r[3],
                    action=json.loads(r[4]), path=json.loads(r[5]),
                    requires_confirmation=r[6], incident_id=r[7], incident_type=r[8])

    def fixes(self):
        rows = self.read("MATCH (a:Agent)-[:PROPOSED]->(f:Fix)-[:RESOLVES]->(i:Incident) RETURN f.id,f.proposal,f.status,f.rejected_rule,f.action,f.path,f.requires_confirmation,i.id,a.name ORDER BY f.created DESC")
        return [dict(id=r[0], proposal=r[1], status=r[2], rejected_rule=r[3],
                     action=json.loads(r[4]), path=json.loads(r[5]),
                     requires_confirmation=r[6], incident_id=r[7], agent=r[8]) for r in rows]

    def incidents(self):
        rows = self.read("MATCH (i:Incident) RETURN i.id,i.type,i.desc,i.ts,i.source,coalesce(i.status,'active') ORDER BY i.ts DESC")
        return [dict(id=r[0], type=r[1], desc=r[2], ts=r[3], source=r[4], status=r[5]) for r in rows]

    def notifications(self):
        rows = self.read("MATCH (n:Notification)-[:TO]->(p:Person) RETURN n.id,n.incident_id,n.fix_id,p.name,n.subject,n.body,n.status ORDER BY n.id DESC")
        return [dict(id=r[0], incident_id=r[1], fix_id=r[2], person=r[3], subject=r[4], body=r[5], status=r[6]) for r in rows]

    def graph_data(self):
        nodes = self.read("MATCH (n) WHERE NOT n:Notification AND (NOT n:Person OR n.role <> 'attendee') RETURN id(n),labels(n),coalesce(n.id,n.name,n.title,n.item),coalesce(n.title,n.name,n.proposal,n.desc,n.item,n.summary),coalesce(n.status,'')")
        edges = self.read("MATCH (a)-[r]->(b) WHERE NOT a:Notification AND NOT b:Notification AND (NOT a:Person OR a.role <> 'attendee') AND (NOT b:Person OR b.role <> 'attendee') RETURN id(a),id(b),type(r)")
        return {"nodes": [dict(id=r[0], labels=r[1], key=r[2], label=str(r[3])[:70], status=r[4]) for r in nodes],
                "links": [dict(source=r[0], target=r[1], type=r[2]) for r in edges]}
