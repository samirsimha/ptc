import json
import os
import re
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
        "MATCH (t:Talk) "
        "OPTIONAL MATCH (speaker:Person)-[:PRESENTS]->(t) "
        "OPTIONAL MATCH (t)-[:IN_SESSION]->(session:Session) "
        "RETURN t.id, t.title, t.start, t.end, t.status, "
        "collect(DISTINCT speaker.name), collect(DISTINCT session.title)",
    ).result_set
    if not talks:
        raise ValueError("Cannot triage an incident because the graph has no talks")

    available_talks = [
        {
            "id": row[0],
            "title": row[1],
            "start": row[2],
            "end": row[3],
            "status": row[4],
            "presenters": [name for name in (row[5] or []) if name],
            "sessions": [title for title in (row[6] or []) if title],
        }
        for row in talks
    ]
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
                    "with incident_type and target_talk_id. Supported incident_type values "
                    "are 'overrun', 'late_start', and 'no_show'. Use a talk id exactly as "
                    "provided. Use 'late_start' when a speaker/talk will begin later than "
                    "scheduled; use 'overrun' when a talk has already started and will run "
                    "longer than scheduled. For an overrun or late_start, return mins as a "
                    "positive whole number. A no-show does not need mins. If the text does "
                    "not clearly describe a supported "
                    "incident involving one listed talk, return "
                    "{\"incident_type\":\"unsupported\"}. Match speaker "
                    "names against the presenters list and phrases about an event block "
                    "against session titles. Choose the talk whose start or duration is "
                    "affected by the incident. If a named presenter has multiple talks in "
                    "one session, use the incident wording, talk title, status, and time to "
                    "select the best matching talk. For a no-show mentioning multiple "
                    "talks, select the title attached to the no-show clause, not a later "
                    "talk the person says they will attend."
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
    incident_type = parsed.get("incident_type")
    normalized_text = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
    explicit_talks = [
        talk for talk in available_talks
        if re.sub(r"[^a-z0-9]+", " ", talk["title"].lower()).strip()
        in normalized_text
    ]
    if len(explicit_talks) == 1:
        target_talk_id = explicit_talks[0]["id"]

    normalized_titles = {
        talk["id"]: re.sub(r"[^a-z0-9]+", " ", talk["title"].lower()).strip()
        for talk in explicit_talks
    }
    no_show_clause = re.search(
        r"\b(?:no[\s-]?show|did not show(?: up)?|didn't show(?: up)?)"
        r"\s+(?:for|at)\s+(?:the\s+)?(.+?)(?=\bbut\b|[.;,]|$)",
        normalized_text,
    )
    if no_show_clause:
        clause = no_show_clause.group(1)
        clause_talks = [
            talk for talk in explicit_talks
            if normalized_titles[talk["id"]] in clause
        ]
        if len(clause_talks) == 1:
            target_talk_id = clause_talks[0]["id"]

    no_show_cue = re.search(
        r"\b(no[\s-]?show|did not show|didn't show|absent)\b", text, re.I
    )
    late_start_cue = (
        re.search(r"\b(?:start|begin)\w*\b.{0,100}\b(?:late|later)\b", text, re.I)
        or re.search(r"\brunning\s+\d+\s+(?:minutes?|mins?)\s+late\b", text, re.I)
    )
    if late_start_cue and not no_show_cue:
        incident_type = "late_start"
        minutes_match = re.search(
            r"\b(\d+)\s*(?:minutes?|mins?)\s+(?:late|later)\b", text, re.I
        )
        if minutes_match:
            mins = int(minutes_match.group(1))

    if incident_type not in {"overrun", "late_start", "no_show"}:
        raise ValueError("Triage could not identify a supported overrun incident")
    if target_talk_id not in talk_ids:
        raise ValueError("Triage selected a talk that does not exist in the graph")
    if incident_type in {"overrun", "late_start"} and (
        not isinstance(mins, int) or isinstance(mins, bool) or mins <= 0
    ):
        raise ValueError("Triage must return a positive whole number of delay minutes")

    incident = {"type": incident_type, "target_talk_id": target_talk_id}
    if incident_type in {"overrun", "late_start"}:
        incident["mins"] = mins
    return incident

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
        "RETURN talk.id, talk.title, talk.start, talk.end, room.id, session.id"
    ).result_set:
        talk = talks.setdefault(row[0], {
            "id": row[0],
            "title": row[1],
            "start": row[2],
            "end": row[3],
            "room_id": row[4],
            "session_ids": set(),
        })
        if row[4] is not None:
            talk["room_id"] = row[4]
        if row[5] is not None:
            talk["session_ids"].add(row[5])

    sessions = {}
    for row in g.query(
        "MATCH (session:Session) "
        "OPTIONAL MATCH (talk:Talk)-[:IN_SESSION]->(session) "
        "RETURN session.id, session.title, session.start, session.end, "
        "session.subfield, talk.id"
    ).result_set:
        session = sessions.setdefault(row[0], {
            "id": row[0],
            "title": row[1],
            "start": row[2],
            "end": row[3],
            "subfield": row[4],
            "talk_ids": set(),
        })
        if row[5] is not None:
            session["talk_ids"].add(row[5])

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
            changes.append({
                "session_id": session["id"],
                "session_title": session["title"],
                "start": start,
                "end": end,
            })
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


# Find a safe timing plan, using AI only to choose among validated alternatives.
def _schedule_timing_change(g, incident, client=None):
    if incident["type"] not in {"overrun", "late_start"}:
        raise ValueError(f"Unsupported incident type: {incident['type']}")

    affected_talks = tools.overrun_cascade(
        g,
        incident["target_talk_id"],
        incident["mins"],
    )

    talks, sessions, presenters, attendees = _schedule_snapshot(g)
    target_talk = talks[incident["target_talk_id"]]
    is_late_start = incident["type"] == "late_start"
    base_changes = [{
        "talk_id": target_talk["id"],
        "start": target_talk["start"] + (incident["mins"] if is_late_start else 0),
        "end": target_talk["end"] + incident["mins"],
        **({"status": "scheduled"} if is_late_start else {}),
    }] + [
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
    following_count = len(staged_changes) - 1
    following_label = (
        f"{following_count} following talk"
        f"{'s' if following_count != 1 else ''}"
    )
    speaker_rows = g.query(
        "MATCH (speaker:Person)-[:PRESENTS]->(talk:Talk {id: $talk_id}) "
        "RETURN speaker.name ORDER BY speaker.name",
        {"talk_id": target_talk["id"]},
    ).result_set
    speaker_label = f"{speaker_rows[0][0]}'s " if speaker_rows else ""
    action = (
        f"Delay {speaker_label}{target_talk['title']} by {incident['mins']} minutes and "
        if is_late_start
        else f"Extend {speaker_label}{target_talk['title']} by {incident['mins']} minutes and "
    )
    tail = (
        f"reschedule {following_label} to avoid conflicts"
        if selected_candidate
        else f"shift {following_label}"
    )
    proposal = action + tail
    schema.make_node(
        g,
        "Fix",
        id=fix_id,
        proposal=proposal,
        status="proposed",
        staged_changes=json.dumps(staged_changes),
        staged_session_changes=json.dumps(staged_session_changes),
        staged_presenter_change=json.dumps(None),
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


def schedule_overrun(g, incident, client=None):
    if incident["type"] != "overrun":
        raise ValueError(f"Unsupported incident type: {incident['type']}")
    return _schedule_timing_change(g, incident, client)


def schedule_late_start(g, incident, client=None):
    if incident["type"] != "late_start":
        raise ValueError(f"Unsupported incident type: {incident['type']}")
    return _schedule_timing_change(g, incident, client)


# Propose the highest-ranked checked-in expert as a no-show speaker substitute.
def schedule_no_show(g, incident):
    if incident["type"] != "no_show":
        raise ValueError(f"Unsupported incident type: {incident['type']}")

    talk_id = incident["target_talk_id"]
    speaker_rows = g.query(
        "MATCH (speaker:Person)-[:PRESENTS]->(talk:Talk {id: $talk_id}) "
        "RETURN speaker.id, speaker.name",
        {"talk_id": talk_id},
    ).result_set
    if not speaker_rows:
        raise ValueError(f"No scheduled presenter found for talk: {talk_id}")

    substitutes = tools.find_substitutes(g, talk_id)
    if not substitutes:
        raise ValueError(f"No checked-in, available expert found for talk: {talk_id}")

    original_speaker_id, original_speaker_name = speaker_rows[0]
    substitute = substitutes[0]
    if substitute["id"] == original_speaker_id:
        raise ValueError("The scheduled speaker cannot substitute for themself")

    talk_rows = g.query(
        "MATCH (talk:Talk {id: $talk_id}) RETURN talk.title",
        {"talk_id": talk_id},
    ).result_set
    fix_id = f"fix-{uuid4().hex}"
    presenter_change = {
        "talk_id": talk_id,
        "from_person_id": original_speaker_id,
        "from_person": original_speaker_name,
        "to_person_id": substitute["id"],
        "to_person": substitute["name"],
    }
    proposal = (
        f"Replace absent speaker {original_speaker_name} with "
        f"{substitute['name']} for {talk_rows[0][0]}"
    )
    schema.make_node(
        g,
        "Fix",
        id=fix_id,
        proposal=proposal,
        status="proposed",
        staged_changes=json.dumps([]),
        staged_session_changes=json.dumps([]),
        staged_presenter_change=json.dumps(presenter_change),
    )
    schema.link(g, "Fix", fix_id, "RESOLVES", "Incident", incident["incident_id"])
    schema.link(g, "Agent", "agent-schedule", "PROPOSED", "Fix", fix_id)
    return {
        "id": fix_id,
        "proposal": proposal,
        "status": "proposed",
        "staged_changes": [],
        "staged_session_changes": [],
        "presenter_change": presenter_change,
        "substitute_is_coauthor": substitute["is_coauthor"],
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
                    "presenter_change": None,
                })
                recipient["changes"][talk_id] = {
                    "title": talk_title,
                    "start": change["start"],
                    "end": change["end"],
                }

    presenter_change = fix.get("presenter_change")
    if presenter_change:
        talk_rows = g.query(
            "MATCH (t:Talk {id: $talk_id}) RETURN t.title",
            {"talk_id": presenter_change["talk_id"]},
        ).result_set
        talk_title = talk_rows[0][0]
        for person_id, person_name in (
            (presenter_change["from_person_id"], presenter_change["from_person"]),
            (presenter_change["to_person_id"], presenter_change["to_person"]),
        ):
            recipient = recipients.setdefault(person_id, {
                "name": person_name,
                "changes": {},
                "presenter_change": None,
            })
            recipient["presenter_change"] = {
                "talk_title": talk_title,
                "from_person": presenter_change["from_person"],
                "to_person": presenter_change["to_person"],
            }

    notifications = []
    for person_id, recipient in recipients.items():
        messages = [
            f"{change['title']} to {change['start'] // 60:02d}:"
            f"{change['start'] % 60:02d}–{change['end'] // 60:02d}:"
            f"{change['end'] % 60:02d}"
            for change in recipient["changes"].values()
        ]
        speaker_change = recipient.get("presenter_change")
        if speaker_change:
            messages.append(
                f"the presenter for {speaker_change['talk_title']} from "
                f"{speaker_change['from_person']} to {speaker_change['to_person']}"
            )
        proposed_changes = " and ".join(messages)
        notification_id = f"notification-{uuid4().hex}"
        if speaker_change:
            body = (
                f"Draft: The presenter for {speaker_change['talk_title']} is proposed "
                f"to change from {speaker_change['from_person']} to "
                f"{speaker_change['to_person']}. The assignment will change only if "
                "the organizer approves the fix."
            )
        else:
            body = (
                f"Draft: {proposed_changes} is proposed to change. "
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
    if incident["type"] == "overrun":
        fix = schedule_overrun(g, incident, client)
    elif incident["type"] == "late_start":
        fix = schedule_late_start(g, incident, client)
    elif incident["type"] == "no_show":
        fix = schedule_no_show(g, incident)
    else:
        raise ValueError(f"Unsupported incident type: {incident['type']}")
    notifications = draft_notifications(g, fix, fix["id"])
    return {
        "incident": incident,
        "fix": fix,
        "notifications": notifications,
        "note": "Proposal passed schedule checks; changes await human approval.",
    }
