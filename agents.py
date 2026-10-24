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


# Load the talk, session, speaker, and attendee relationships needed to validate a schedule.
def _schedule_snapshot(g):
    talks = {}
    for row in g.query(
        "MATCH (talk:Talk) "
        "OPTIONAL MATCH (talk)-[:IN_ROOM]->(room:Room) "
        "OPTIONAL MATCH (talk)-[:IN_SESSION]->(session:Session) "
        "RETURN talk.id, talk.start, talk.end, room.id, session.id"
    ).result_set:
        talk = talks.setdefault(row[0], {
            "id": row[0],
            "start": row[1],
            "end": row[2],
            "room_id": row[3],
            "session_ids": set(),
        })
        if row[3] is not None:
            talk["room_id"] = row[3]
        if row[4] is not None:
            talk["session_ids"].add(row[4])

    sessions = {}
    for row in g.query(
        "MATCH (session:Session) "
        "OPTIONAL MATCH (talk:Talk)-[:IN_SESSION]->(session) "
        "RETURN session.id, session.start, session.end, session.subfield, talk.id"
    ).result_set:
        session = sessions.setdefault(row[0], {
            "id": row[0],
            "start": row[1],
            "end": row[2],
            "subfield": row[3],
            "talk_ids": set(),
        })
        if row[4] is not None:
            session["talk_ids"].add(row[4])

    presenters = {}
    for row in g.query(
        "MATCH (person:Person)-[:PRESENTS]->(talk:Talk) "
        "RETURN person.id, talk.id"
    ).result_set:
        presenters.setdefault(row[0], set()).add(row[1])

    attendees = {}
    for row in g.query(
        "MATCH (person:Person)-[:RSVP]->(talk:Talk) "
        "RETURN person.id, talk.id"
    ).result_set:
        attendees.setdefault(row[0], set()).add(row[1])

    return talks, sessions, presenters, attendees


# Expand or shift touched sessions so all their scheduled talks remain inside their session bounds.
def _session_changes(talks, sessions, talk_changes):
    changed_talks = {change["talk_id"]: change for change in talk_changes}
    changes = []
    for session in sessions.values():
        member_ids = session["talk_ids"]
        changed_members = member_ids & changed_talks.keys()
        if not changed_members:
            continue

        deltas = {
            changed_talks[talk_id]["start"] - talks[talk_id]["start"]
            for talk_id in changed_members
        }
        if changed_members == member_ids and len(deltas) == 1:
            delta = next(iter(deltas))
            start, end = session["start"] + delta, session["end"] + delta
        else:
            start = min(
                [session["start"]]
                + [changed_talks[talk_id]["start"] for talk_id in changed_members]
            )
            end = max(
                [session["end"]]
                + [changed_talks[talk_id]["end"] for talk_id in changed_members]
            )

        if start != session["start"] or end != session["end"]:
            changes.append({"session_id": session["id"], "start": start, "end": end})
    return changes


# Reject candidate plans with room, speaker, RSVP, or same-subfield session conflicts.
def _schedule_conflicts(talks, sessions, presenters, attendees, talk_changes, session_changes):
    changed = {change["talk_id"] for change in talk_changes}
    times = {
        talk_id: {
            "start": change["start"],
            "end": change["end"],
        }
        for talk_id, change in ((item["talk_id"], item) for item in talk_changes)
    }
    for talk_id, talk in talks.items():
        times.setdefault(talk_id, {"start": talk["start"], "end": talk["end"]})

    conflicts = []
    talk_ids = list(talks)
    for index, first_id in enumerate(talk_ids):
        first = talks[first_id]
        for second_id in talk_ids[index + 1:]:
            second = talks[second_id]
            if first["room_id"] and first["room_id"] == second["room_id"]:
                if (
                    (first_id in changed or second_id in changed)
                    and times[first_id]["start"] < times[second_id]["end"]
                    and times[first_id]["end"] > times[second_id]["start"]
                ):
                    conflicts.append({
                        "kind": "room",
                        "talk_ids": [first_id, second_id],
                        "message": f"{first_id} overlaps {second_id} in {first['room_id']}",
                    })

    for people, kind in ((presenters, "speaker"), (attendees, "attendee")):
        for person_id, person_talks in people.items():
            person_talk_ids = list(person_talks)
            for index, first_id in enumerate(person_talk_ids):
                for second_id in person_talk_ids[index + 1:]:
                    if (
                        (first_id in changed or second_id in changed)
                        and times[first_id]["start"] < times[second_id]["end"]
                        and times[first_id]["end"] > times[second_id]["start"]
                    ):
                        conflicts.append({
                            "kind": kind,
                            "talk_ids": [first_id, second_id],
                            "message": f"{person_id} has an overlap between {first_id} and {second_id}",
                        })

    changed_sessions = {change["session_id"]: change for change in session_changes}
    for session_id, change in changed_sessions.items():
        session = sessions[session_id]
        for talk_id in session["talk_ids"]:
            talk_time = times[talk_id]
            if talk_time["start"] < change["start"] or talk_time["end"] > change["end"]:
                conflicts.append({
                    "kind": "session_bounds",
                    "talk_ids": [talk_id],
                    "message": f"{talk_id} falls outside session {session_id}",
                })
        for other_id, other in sessions.items():
            if other_id == session_id or other["subfield"] != session["subfield"]:
                continue
            other_change = changed_sessions.get(other_id)
            other_start = other_change["start"] if other_change else other["start"]
            other_end = other_change["end"] if other_change else other["end"]
            if change["start"] < other_end and change["end"] > other_start:
                conflicts.append({
                    "kind": "session",
                    "talk_ids": list(session["talk_ids"]),
                    "message": f"session {session_id} overlaps same-subfield session {other_id}",
                })

    return conflicts


# Create a small set of validated later slots for the talk causing the blocking conflict.
def _alternative_plans(talks, sessions, presenters, attendees, base_changes, conflicts):
    affected_ids = {change["talk_id"] for change in base_changes}
    blockers = {
        talk_id
        for conflict in conflicts
        if conflict["kind"] in {"room", "speaker", "attendee"}
        for talk_id in conflict["talk_ids"]
        if talk_id not in affected_ids
    }
    if len(blockers) != 1:
        return []

    blocker_id = next(iter(blockers))
    blocker = talks[blocker_id]
    if len(blocker["session_ids"]) != 1:
        return []
    session_id = next(iter(blocker["session_ids"]))
    session = sessions[session_id]
    session_talk_ids = session["talk_ids"]
    if session_talk_ids & affected_ids:
        return []

    duration = blocker["end"] - blocker["start"]
    candidates = []
    for candidate_start in range(blocker["start"] + 15, 1021 - duration, 15):
        delta = candidate_start - blocker["start"]
        talk_changes = list(base_changes)
        for talk_id in session_talk_ids:
            talk = talks[talk_id]
            talk_changes.append({
                "talk_id": talk_id,
                "start": talk["start"] + delta,
                "end": talk["end"] + delta,
            })
        session_changes = _session_changes(talks, sessions, talk_changes)
        if _schedule_conflicts(
            talks,
            sessions,
            presenters,
            attendees,
            talk_changes,
            session_changes,
        ):
            continue

        candidates.append({
            "id": f"candidate-{len(candidates) + 1}",
            "talk_changes": talk_changes,
            "session_changes": session_changes,
        })
        if len(candidates) == 12:
            break
    return candidates


# Use the model to select only from schedule alternatives already validated by code.
def _choose_schedule_candidate(g, incident, candidates, client):
    if client is None:
        if not os.environ.get("OPENAI_API_KEY"):
            raise ModelConfigurationError(
                "OPENAI_API_KEY is not configured for schedule alternatives"
            )
        client = OpenAI()

    response = client.chat.completions.create(
        model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": (
                    "You are the conference Schedule agent. Select the least disruptive "
                    "valid candidate that resolves the incident. Return only JSON with "
                    "candidate_id matching one supplied candidate. Never invent or edit "
                    "schedule changes."
                ),
            },
            {
                "role": "user",
                "content": json.dumps({
                    "incident": incident,
                    "candidates": candidates,
                }),
            },
        ],
    )
    content = response.choices[0].message.content
    if not content:
        raise ValueError("Schedule model returned an empty response")
    try:
        selected = json.loads(content).get("candidate_id")
    except json.JSONDecodeError as error:
        raise ValueError("Schedule model returned invalid JSON") from error
    candidate = next((item for item in candidates if item["id"] == selected), None)
    if candidate is None:
        raise ValueError("Schedule model selected an unavailable candidate")
    return candidate


# Find a safe overrun schedule, using AI to choose a validated alternative when needed.
def schedule_overrun(g, incident, client=None):
    if incident["type"] != "overrun":
        raise ValueError(f"Unsupported incident type: {incident['type']}")

    affected_talks = tools.overrun_cascade(
        g,
        incident["target_talk_id"],
        incident["mins"],
    )
    if not affected_talks:
        raise ValueError("The overrun has no downstream talks to reschedule")

    talks, sessions, presenters, attendees = _schedule_snapshot(g)
    base_changes = [
        {
            "talk_id": talk["id"],
            "start": talk["projected_start"],
            "end": talk["projected_end"],
        }
        for talk in affected_talks
    ]
    base_session_changes = _session_changes(talks, sessions, base_changes)
    conflicts = _schedule_conflicts(
        talks,
        sessions,
        presenters,
        attendees,
        base_changes,
        base_session_changes,
    )
    selected_candidate = None
    if conflicts:
        candidates = _alternative_plans(
            talks,
            sessions,
            presenters,
            attendees,
            base_changes,
            conflicts,
        )
        if not candidates:
            raise ValueError(
                "No safe schedule alternative was found: "
                + conflicts[0]["message"]
            )
        selected_candidate = _choose_schedule_candidate(g, incident, candidates, client)
        staged_changes = selected_candidate["talk_changes"]
        staged_session_changes = selected_candidate["session_changes"]
    else:
        staged_changes = base_changes
        staged_session_changes = base_session_changes

    fix_id = f"fix-{uuid4().hex}"
    proposal = (
        f"Delay {len(staged_changes)} talk(s) by "
        f"{incident['mins']} minutes"
        if selected_candidate is None
        else f"Reschedule {len(staged_changes)} talk(s) to resolve the overrun conflicts"
    )
    schema.make_node(
        g,
        "Fix",
        id=fix_id,
        proposal=proposal,
        status="proposed",
        staged_changes=json.dumps(staged_changes),
        staged_session_changes=json.dumps(staged_session_changes),
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
        "staged_session_changes": staged_session_changes,
        "selected_candidate_id": selected_candidate["id"] if selected_candidate else None,
    }


# Draft one notification for each presenter or attendee of a talk in the proposed changes.
def draft_notifications(g, fix, fix_id):
    recipients = {}
    changes_by_talk = {
        change["talk_id"]: change
        for change in fix["staged_changes"]
    }
    for talk_id, change in changes_by_talk.items():
        talk_rows = g.query(
            "MATCH (t:Talk {id: $talk_id}) RETURN t.title",
            {"talk_id": talk_id},
        ).result_set
        if not talk_rows:
            raise ValueError(f"Cannot draft notification for missing talk: {talk_id}")
        talk_title = talk_rows[0][0]

        for relationship in ("PRESENTS", "RSVP"):
            rows = g.query(
                f"MATCH (person:Person)-[:{relationship}]->"
                "(talk:Talk {id: $talk_id}) "
                "RETURN person.id, person.name",
                {"talk_id": talk_id},
            ).result_set
            for person_id, person_name in rows:
                recipient = recipients.setdefault(person_id, {
                    "name": person_name,
                    "changes": {},
                })
                recipient["changes"][talk_id] = {
                    "title": talk_title,
                    "start": change["start"],
                    "end": change["end"],
                }

    notifications = []
    for person_id, recipient in recipients.items():
        moved_talks = ", ".join(
            f"{change['title']} to {change['start'] // 60:02d}:"
            f"{change['start'] % 60:02d}–{change['end'] // 60:02d}:"
            f"{change['end'] % 60:02d}"
            for change in recipient["changes"].values()
        )
        notification_id = f"notification-{uuid4().hex}"
        body = (
            f"Draft: {moved_talks} is proposed to move. "
            "The schedule will change only if the organizer approves the fix."
        )
        schema.make_node(
            g,
            "Notification",
            id=notification_id,
            body=body,
            status="draft",
        )
        schema.link(
            g,
            "Agent",
            "agent-comms",
            "DRAFTED",
            "Notification",
            notification_id,
        )
        schema.link(
            g,
            "Notification",
            notification_id,
            "TO",
            "Person",
            person_id,
        )
        schema.link(
            g,
            "Fix",
            fix_id,
            "HAS_NOTIFICATION",
            "Notification",
            notification_id,
        )
        notifications.append({
            "id": notification_id,
            "body": body,
            "status": "draft",
            "recipient": recipient["name"],
        })
    return notifications


# runs Triage and Schedule agents in order and returns the incident and proposed fix
def run_pipeline(g, text, client=None):
    incident = record_incident(g, text, client)
    fix = schedule_overrun(g, incident, client)
    notifications = draft_notifications(g, fix, fix["id"])
    return {
        "incident": incident,
        "fix": fix,
        "notifications": notifications,
        "note": "Proposal passed schedule checks; changes await human approval.",
    }