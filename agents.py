import json
import os
import time
from uuid import uuid4

from openai import OpenAI

import schema
import tools


class ModelConfigurationError(RuntimeError):
    pass


# uses AI to interpret incident text and returns the details as a dictionary
def parse_incident(g, text, client=None):
    talks = g.query(
        "MATCH (t:Talk) RETURN t.id, t.title",
    ).result_set
    if not talks:
        raise ValueError("Cannot triage an incident because the graph has no talks")

    available_talks = [{"id": row[0], "title": row[1]} for row in talks]
    if client is None:
        if not os.environ.get("OPENAI_API_KEY"):
            raise ModelConfigurationError(
                "OPENAI_API_KEY is not configured in the server environment"
            )
        client = OpenAI()

    response = client.chat.completions.create(
        model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": (
                    "You are the conference incident triage agent. Return a JSON object "
                    "with incident_type, target_talk_id, and mins. Supported incident_type "
                    "is only 'overrun'. Use a talk id exactly as provided. For an overrun, "
                    "mins must be the positive number of minutes. If the text does not "
                    "clearly describe a supported incident involving one listed talk, "
                    "return {\"incident_type\":\"unsupported\"}."
                ),
            },
            {
                "role": "user",
                "content": json.dumps({
                    "incident_text": text,
                    "available_talks": available_talks,
                }),
            },
        ],
    )
    content = response.choices[0].message.content
    if not content:
        raise ValueError("Triage model returned an empty response")

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as error:
        raise ValueError("Triage model returned invalid JSON") from error

    target_talk_id = parsed.get("target_talk_id")
    mins = parsed.get("mins")
    talk_ids = {talk["id"] for talk in available_talks}
    if parsed.get("incident_type") != "overrun":
        raise ValueError("Triage could not identify a supported overrun incident")
    if target_talk_id not in talk_ids:
        raise ValueError("Triage selected a talk that does not exist in the graph")
    if not isinstance(mins, int) or isinstance(mins, bool) or mins <= 0:
        raise ValueError("Triage must return a positive whole number of overrun minutes")

    return {
        "type": "overrun",
        "target_talk_id": target_talk_id,
        "mins": mins,
    }

# uses output from parse_incident to find the talk_id and create an incident node in graph
def record_incident(g, text, client=None):
    incident = parse_incident(g, text, client)
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


# finds talks affected from an overrun, create a fix node, and link it to the incident node
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


# runs Triage and Schedule agents in order and returns the incident and proposed fix
def run_pipeline(g, text, client=None):
    incident = record_incident(g, text, client)
    fix = schedule_overrun(g, incident)
    return {
        "incident": incident,
        "fix": fix,
        "note": "Draft proposal only; schedule and attendee conflicts are not yet validated.",
    }