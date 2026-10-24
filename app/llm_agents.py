"""Optional prompt-and-Cypher-tool layer for each graph-sharing agent."""
from __future__ import annotations

import json
import os

from .db import Store, now, uid


TOOLS = {
    "incident": "MATCH (i:Incident {id:$id}) RETURN i.type,i.desc,i.ts,i.source",
    "affected": "MATCH (i:Incident {id:$id})-[:AFFECTS]->(n) RETURN labels(n),coalesce(n.id,n.name,n.title),coalesce(n.title,n.name,n.item) LIMIT 40",
    "fixes": "MATCH (f:Fix)-[:RESOLVES]->(i:Incident {id:$id}) RETURN f.proposal,f.status,f.rejected_rule LIMIT 20",
    "cascade": "MATCH (i:Incident {id:$id})-[:CAUSED]->(child:Incident) RETURN child.type,count(child) ORDER BY count(child) DESC",
    "rooms": "MATCH (r:Room) RETURN r.name,r.wing,r.capacity,r.has_power",
    "event": "MATCH (i:Incident {id:$id})-[:AFFECTS]->(e:SideEvent) OPTIONAL MATCH (p:Person)-[:RSVP]->(e) RETURN e.name,e.capacity,count(DISTINCT p)",
    "memory": "MATCH (p:Person)-[:PRESENTS]->(:Talk)<-[:AFFECTS]-(i:Incident {type:'overrun'}) RETURN p.name,count(i) ORDER BY count(i) DESC LIMIT 10",
    "drafts": "MATCH (i:Incident {id:$id})-[:DRAFTED]->(n:Notification) RETURN count(n)",
}

PROMPTS = {
    "Triage": "You are the triage agent. Check how the incident was classified and which graph nodes it affects.",
    "Schedule": "You are the schedule agent. Inspect affected talks, cascades, and proposed schedule fixes.",
    "Logistics": "You are the logistics agent. Inspect capacity, rooms, catering, and child incidents after schedule work.",
    "Comms": "You are the communications agent. Inspect the affected nodes and proposals before drafting targeted notices.",
    "Coordinator": "You are the coordinator. Inspect proposals and named rule rejections. The human must approve any change.",
}


def agent_observation(store: Store, agent: str, incident_id: str, fallback: str) -> str:
    """Ask a model to call a constrained read-only Cypher tool, then summarize."""
    if os.getenv("OPENAI_AGENTS", "0") != "1" or not os.getenv("OPENAI_API_KEY"):
        return fallback
    try:
        from openai import OpenAI

        client = OpenAI()
        messages = [
            {"role": "system", "content": (PROMPTS[agent] + " Use graph_read before answering. The returned rows are authoritative. You cannot write to the graph. Summarize the returned rows in one factual, past-tense sentence. If rows are present, do not claim there is no information. Do not recommend an action rejected by a rule.")},
            {"role": "user", "content": f"Review incident {incident_id}."},
        ]
        view = {"Triage": "affected", "Schedule": "fixes", "Comms": "drafts", "Coordinator": "fixes"}.get(agent)
        if agent == "Logistics":
            kind = store.read(TOOLS["incident"], id=incident_id)[0][0]
            view = "event" if kind == "oversold" else "cascade"
        tools = [{"type": "function", "function": {"name": "graph_read", "description": f"Read the {view} Cypher view of the shared FalkorDB graph", "parameters": {"type": "object", "properties": {"view": {"type": "string", "enum": [view]}}, "required": ["view"], "additionalProperties": False}}}]
        first = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"), messages=messages,
            tools=tools, tool_choice="required", timeout=8,
        )
        call_message = first.choices[0].message
        if not call_message.tool_calls:
            return fallback
        messages.append(call_message.model_dump(exclude_none=True))
        observed = []
        for call in call_message.tool_calls[:3]:
            try:
                rows = store.read(TOOLS[view], id=incident_id)
                observed.extend(rows)
                content = json.dumps({"view": view, "rows": rows}, default=str)
            except Exception as exc:
                content = json.dumps({"error": str(exc)})
            messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
        second = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"), messages=messages,
            tools=tools, tool_choice="none", timeout=8,
        )
        summary = (second.choices[0].message.content or fallback)[:400]
        if observed and any(phrase in summary.lower() for phrase in ("no information", "no drafts", "no data", "nothing to review")):
            return fallback
        return summary
    except Exception:
        return fallback


def record_step(store: Store, agent: str, incident_id: str, fallback: str):
    observation = agent_observation(store, agent, incident_id, fallback)
    store.q("""
        MATCH (a:Agent {name:$agent}), (i:Incident {id:$incident})
        CREATE (run:AgentRun {id:$id, agent:$agent, incident_id:$incident,
          summary:$summary, model_observation:$observation, ts:$ts})
        CREATE (a)-[:RAN]->(run), (run)-[:REVIEWS]->(i)
    """, agent=agent, incident=incident_id, id=uid("run"), summary=fallback,
         observation=observation if observation != fallback else "", ts=now())
