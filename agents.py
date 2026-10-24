import json
import re
import time
from uuid import uuid4

import schema
import tools

# interprets text which states that keynote has ran x minutes over and returns as a dictionary
def parse_incident(text):
    match = re.fullmatch(
        r"\s*keynote\s+running\s+(\d+)\s+over\s*",
        text,
        re.IGNORECASE,
    )
    if not match:
        raise ValueError("Expected an incident like: keynote running 20 over")

    return {
        "type": "overrun",
        "target_talk_id": "talk-keynote",
        "mins": int(match.group(1)),
    }

# matches output from parse_incident to a talk_id in graph and creates an incident node in graph
def record_incident(g, text):
    incident = parse_incident(text)
    talk_rows = g.query(
        "MATCH (t:Talk {id: $talk_id}) RETURN t.id",
        {"talk_id": incident["target_talk_id"]},
    ).result_set
    if not talk_rows:
        raise ValueError(f"Talk not found: {incident['target_talk_id']}")

    incident_id = f"incident-{uuid4().hex}"
    schema.make_node(
        g,
        "Incident",
        id=incident_id,
        type=incident["type"],
        desc=text,
        ts=int(time.time()),
    )
    schema.link(
        g,
        "Incident",
        incident_id,
        "AFFECTS",
        "Talk",
        incident["target_talk_id"],
    )
    return {**incident, "incident_id": incident_id}


# finds talks affected from an overrun and 
def schedule_overrun(g, incident):
    if incident["type"] != "overrun":
        raise ValueError(f"Unsupported incident type: {incident['type']}")

    affected_talks = tools.overrun_cascade(
        g,
        incident["target_talk_id"],
        incident["mins"],
    )
    if not affected_talks:
        raise ValueError("The overrun has no downstream talks to reschedule")

    fix_id = f"fix-{uuid4().hex}"
    staged_changes = [
        {
            "talk_id": talk["id"],
            "start": talk["projected_start"],
            "end": talk["projected_end"],
        }
        for talk in affected_talks
    ]
    proposal = (
        f"Delay {len(staged_changes)} downstream talk(s) by "
        f"{incident['mins']} minutes"
    )
    schema.make_node(
        g,
        "Fix",
        id=fix_id,
        proposal=proposal,
        status="proposed",
        staged_changes=json.dumps(staged_changes),
    )
    schema.link(
        g,
        "Fix",
        fix_id,
        "RESOLVES",
        "Incident",
        incident["incident_id"],
    )
    schema.link(
        g,
        "Agent",
        "agent-schedule",
        "PROPOSED",
        "Fix",
        fix_id,
    )
    return {
        "id": fix_id,
        "proposal": proposal,
        "status": "proposed",
        "staged_changes": staged_changes,
        "attendee_conflicts": {
            talk["id"]: talk["attendee_conflicts"]
            for talk in affected_talks
            if talk["attendee_conflicts"]
        },
    }