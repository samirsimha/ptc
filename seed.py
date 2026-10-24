import schema


# Create a repeatable conference graph for testing the incident scenarios.
def seed(g):
    schema.reset(g)

    rooms = [
        {"id": "room-west-main", "name": "West Hall", "capacity": 350, "wing": "West", "has_power": True},
        {"id": "room-west-lab", "name": "West Workshop", "capacity": 180, "wing": "West", "has_power": True},
        {"id": "room-east-main", "name": "East Hall", "capacity": 160, "wing": "East", "has_power": True},
        {"id": "room-east-lab", "name": "East Lab", "capacity": 120, "wing": "East", "has_power": True},
    ]
    sessions = [
        {"id": "session-opening", "title": "Opening and Keynote", "subfield": "AI", "start": 540, "end": 630},
        {"id": "session-workshop", "title": "Hands-on Workshop", "subfield": "Systems", "start": 690, "end": 720},
        {"id": "session-afternoon", "title": "Afternoon Showcase", "subfield": "Systems", "start": 780, "end": 810},
        {"id": "session-neuro-a", "title": "Neuroscience A", "subfield": "Neuroscience", "start": 810, "end": 870},
        {"id": "session-neuro-b", "title": "Neuroscience B", "subfield": "Neuroscience", "start": 825, "end": 885},
        {"id": "session-east", "title": "East Wing Talks", "subfield": "Systems", "start": 720, "end": 750},
        {"id": "session-panel", "title": "Closing Panel", "subfield": "AI", "start": 900, "end": 960},
    ]
    talks = [
        {"id": "talk-early", "title": "A Short History of AI", "start": 540, "end": 570, "status": "completed"},
        {"id": "talk-keynote", "title": "The Future of AI", "start": 570, "end": 600, "status": "scheduled"},
        {"id": "talk-followup", "title": "AI in the Real World", "start": 600, "end": 630, "status": "scheduled"},
        {"id": "talk-workshop", "title": "Reliable Graph Systems", "start": 690, "end": 720, "status": "scheduled"},
        {"id": "talk-gap", "title": "Building Better Data Pipelines", "start": 780, "end": 810, "status": "scheduled"},
        {"id": "talk-noshow", "title": "Neural Signals in Practice", "start": 810, "end": 855, "status": "scheduled"},
        {"id": "talk-neuro-overlap", "title": "Mapping the Human Brain", "start": 825, "end": 855, "status": "scheduled"},
        {"id": "talk-east", "title": "A Systems Field Report", "start": 720, "end": 750, "status": "scheduled"},
        {"id": "talk-panel-first", "title": "Panel: Opening Perspective", "start": 900, "end": 920, "status": "scheduled"},
        {"id": "talk-panel-second", "title": "Panel: Discussion", "start": 920, "end": 960, "status": "scheduled"},
    ]
    people = [
        {"id": "person-maya", "name": "Maya Rivera", "checked_in": True, "role": "speaker"},
        {"id": "person-jules", "name": "Jules Park", "checked_in": True, "role": "speaker"},
        {"id": "person-avery", "name": "Avery Morgan", "checked_in": True, "role": "speaker"},
        {"id": "person-nina", "name": "Nina Patel", "checked_in": False, "role": "speaker"},
        {"id": "person-omar", "name": "Omar Chen", "checked_in": True, "role": "speaker"},
        {"id": "person-lena", "name": "Lena Brooks", "checked_in": True, "role": "speaker"},
        {"id": "person-kai", "name": "Kai Rivers", "checked_in": True, "role": "speaker"},
        {"id": "person-tess", "name": "Tess Nguyen", "checked_in": True, "role": "speaker"},
        {"id": "person-chair", "name": "Sam Ellis", "checked_in": True, "role": "speaker"},
    ]
    topics = [
        {"id": "topic-ai", "name": "Artificial Intelligence"},
        {"id": "topic-systems", "name": "Distributed Systems"},
        {"id": "topic-neuro", "name": "Neuroscience"},
    ]
    side_events = [
        {"id": "event-lab-tour", "name": "Research Lab Tour", "start": 780, "capacity": 100},
    ]
    catering = [
        {"id": "catering-tour", "item": "Boxed lunch", "servings": 100, "time": 780},
    ]
    incidents = [
        {"id": "incident-maya-overrun", "type": "overrun", "desc": "Maya's morning talk previously ran 10 minutes over.", "ts": 570},
        {"id": "incident-panel-overrun", "type": "overrun", "desc": "The first panelist ran 5 minutes over, creating time debt.", "ts": 900},
    ]
    agents = [
        {"id": "agent-triage", "name": "Triage"},
        {"id": "agent-schedule", "name": "Schedule"},
        {"id": "agent-logistics", "name": "Logistics"},
        {"id": "agent-comms", "name": "Comms"},
        {"id": "agent-coordinator", "name": "Coordinator"},
    ]

    for label, records in [
        ("Room", rooms),
        ("Session", sessions),
        ("Talk", talks),
        ("Person", people),
        ("Topic", topics),
        ("SideEvent", side_events),
        ("Catering", catering),
        ("Incident", incidents),
        ("Agent", agents),
    ]:
        for record in records:
            schema.make_node(g, label, **record)

    # Add 300 repeatable RSVPs to make the lab tour visibly oversold.
    for attendee_number in range(1, 301):
        attendee_id = f"attendee-{attendee_number:03}"
        schema.make_node(
            g,
            "Person",
            id=attendee_id,
            name=f"Attendee {attendee_number:03}",
            checked_in=True,
            role="attendee",
        )
        schema.link(g, "Person", attendee_id, "RSVP", "SideEvent", "event-lab-tour")

    relationships = [
        ("Person", "person-maya", "PRESENTS", "Talk", "talk-early", {}),
        ("Person", "person-maya", "PRESENTS", "Talk", "talk-keynote", {}),
        ("Person", "person-jules", "PRESENTS", "Talk", "talk-followup", {}),
        ("Person", "person-avery", "PRESENTS", "Talk", "talk-workshop", {}),
        ("Person", "person-kai", "PRESENTS", "Talk", "talk-gap", {}),
        ("Person", "person-nina", "PRESENTS", "Talk", "talk-noshow", {}),
        ("Person", "person-tess", "PRESENTS", "Talk", "talk-neuro-overlap", {}),
        ("Person", "person-avery", "PRESENTS", "Talk", "talk-east", {}),
        ("Person", "person-kai", "PRESENTS", "Talk", "talk-panel-first", {}),
        ("Person", "person-chair", "PRESENTS", "Talk", "talk-panel-second", {}),
        ("Person", "person-chair", "CHAIRS", "Session", "session-panel", {}),
        ("Person", "person-lena", "CHAIRS", "Session", "session-neuro-a", {}),
        ("Person", "person-nina", "COAUTHORED", "Person", "person-omar", {}),
        ("Person", "person-omar", "EXPERT_IN", "Topic", "topic-neuro", {}),
        ("Incident", "incident-maya-overrun", "AFFECTS", "Talk", "talk-early", {}),
        ("Incident", "incident-panel-overrun", "AFFECTS", "Talk", "talk-panel-first", {}),
        ("Catering", "catering-tour", "FOR", "SideEvent", "event-lab-tour", {}),
        ("Talk", "talk-early", "NEXT_IN", "Talk", "talk-keynote", {}),
        ("Talk", "talk-keynote", "NEXT_IN", "Talk", "talk-followup", {}),
        ("Talk", "talk-workshop", "NEXT_IN", "Talk", "talk-gap", {}),
        ("Talk", "talk-east", "NEXT_IN", "Talk", "talk-neuro-overlap", {}),
        ("Talk", "talk-panel-first", "NEXT_IN", "Talk", "talk-panel-second", {}),
    ]
    for talk_id, room_id in [
        ("talk-early", "room-west-main"),
        ("talk-keynote", "room-west-main"),
        ("talk-followup", "room-west-main"),
        ("talk-workshop", "room-west-lab"),
        ("talk-gap", "room-west-lab"),
        ("talk-noshow", "room-east-main"),
        ("talk-neuro-overlap", "room-east-lab"),
        ("talk-east", "room-east-lab"),
        ("talk-panel-first", "room-west-lab"),
        ("talk-panel-second", "room-west-lab"),
    ]:
        relationships.append(("Talk", talk_id, "IN_ROOM", "Room", room_id, {}))

    for talk_id, session_id, order in [
        ("talk-early", "session-opening", 1),
        ("talk-keynote", "session-opening", 2),
        ("talk-followup", "session-opening", 3),
        ("talk-workshop", "session-workshop", 1),
        ("talk-gap", "session-afternoon", 1),
        ("talk-noshow", "session-neuro-a", 1),
        ("talk-neuro-overlap", "session-neuro-b", 1),
        ("talk-east", "session-east", 1),
        ("talk-panel-first", "session-panel", 1),
        ("talk-panel-second", "session-panel", 2),
    ]:
        relationships.append(("Talk", talk_id, "IN_SESSION", "Session", session_id, {"order": order}))

    for talk_id, topic_id in [
        ("talk-early", "topic-ai"),
        ("talk-keynote", "topic-ai"),
        ("talk-followup", "topic-ai"),
        ("talk-workshop", "topic-systems"),
        ("talk-gap", "topic-systems"),
        ("talk-noshow", "topic-neuro"),
        ("talk-neuro-overlap", "topic-neuro"),
        ("talk-east", "topic-systems"),
        ("talk-panel-first", "topic-ai"),
        ("talk-panel-second", "topic-ai"),
    ]:
        relationships.append(("Talk", talk_id, "ABOUT", "Topic", topic_id, {}))

    for from_label, from_id, rel, to_label, to_id, props in relationships:
        schema.link(g, from_label, from_id, rel, to_label, to_id, **props)
