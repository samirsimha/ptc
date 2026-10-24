"""Deterministic conference with deliberate operational failure cases."""
from __future__ import annotations

import random

from .db import Store


ROOMS = [
    dict(id="room-east-hall", name="East Hall", capacity=120, wing="east", has_power=True),
    dict(id="room-east-studio", name="East Studio", capacity=80, wing="east", has_power=True),
    dict(id="room-west-auditorium", name="West Auditorium", capacity=250, wing="west", has_power=True),
    dict(id="room-west-lab", name="West Lab", capacity=160, wing="west", has_power=True),
]
SESSIONS = [
    dict(id="session-keynote", title="Keynote and AI Futures", subfield="AI", start=540, end=660, room="room-east-hall", panel=False),
    dict(id="session-nlp", title="Language and Society", subfield="NLP", start=540, end=690, room="room-west-auditorium", panel=False),
    dict(id="session-robotics", title="Robotics Frontiers", subfield="Robotics", start=675, end=795, room="room-east-studio", panel=False),
    dict(id="session-nlp-panel", title="NLP Methods Panel", subfield="NLP", start=810, end=930, room="room-east-hall", panel=True),
    dict(id="session-systems", title="Reliable ML Systems", subfield="Systems", start=810, end=930, room="room-west-lab", panel=False),
    dict(id="session-vision-panel", title="Vision and Access Panel", subfield="Vision", start=945, end=1065, room="room-east-studio", panel=True),
    dict(id="session-nlp-overflow", title="NLP Late-Breaking Talks", subfield="NLP", start=960, end=1020, room="room-west-lab", panel=False),
]
TOPICS = ["AI", "NLP", "Robotics", "Systems", "Vision", "Accessibility"]
SPEAKERS = [
    "Dr. Ada Reyes", "Dr. Theo Park", "Dr. Naomi Chen", "Dr. Luis Moreno",
    "Dr. Imani Okafor", "Dr. Priya Shah", "Dr. Eli Brooks", "Dr. Fatima Ali",
    "Dr. Hugo Silva", "Dr. Zoe King", "Dr. Ben Ortiz", "Dr. Leila Haddad",
    "Dr. Nina Patel", "Dr. Mira Solis", "Dr. Evan Wright", "Dr. Asha Menon",
    "Dr. Omar Nasser", "Dr. June Kim", "Dr. Rosa Vega", "Dr. Samir Rao",
]
TITLES = [
    "Keynote: AI in the Wild", "Learning with Human Feedback", "Scientific Agents", "Evaluation Under Shift",
    "Community Language Models", "Low Resource Translation", "Conversational Safety", "Language for All",
    "Robots in Shared Spaces", "Tactile Navigation", "Reliable Manipulation", "Field Robotics",
    "Panel: Responsible NLP", "Multilingual Methods", "Panel: Open Benchmarks", "The Future of Text",
    "Production ML", "Data Pipelines at Scale", "Observability for Models", "Keeping Systems Running",
    "Panel: Vision Access", "Assistive Perception", "Vision in Practice", "Panel: Open Questions",
]


def batch(store: Store, label: str, rows: list[dict]):
    if not rows:
        return
    fields = sorted(rows[0])
    props = ", ".join(f"{field}: row.{field}" for field in fields)
    store.q(f"UNWIND $rows AS row CREATE (n:{label} {{{props}}})", rows=rows)


def edge_batch(store: Store, left: str, rel: str, right: str, rows: list[dict], props: list[str] | None = None):
    if not rows:
        return
    attrs = " {" + ", ".join(f"{p}: row.{p}" for p in props) + "}" if props else ""
    store.q(f"UNWIND $rows AS row MATCH (a:{left} {{id:row.a}}), (b:{right} {{id:row.b}}) CREATE (a)-[:{rel}{attrs}]->(b)", rows=rows)


def seed(store: Store, reset: bool = False):
    if reset:
        store.reset()
    elif store.graph_name in store.db.list_graphs() and store.read("MATCH (r:Room) RETURN r.id LIMIT 1"):
        return {"seeded": False, "reason": "graph already populated"}
    rng = random.Random(1426)
    batch(store, "Room", ROOMS)
    batch(store, "Session", [{k: v for k, v in s.items() if k != "room"} for s in SESSIONS])
    edge_batch(store, "Session", "IN_ROOM", "Room", [dict(a=s["id"], b=s["room"]) for s in SESSIONS])
    batch(store, "Topic", [dict(id=f"topic-{t.lower()}", name=t) for t in TOPICS])
    batch(store, "Agent", [dict(id=f"agent-{a.lower()}", name=a) for a in ("Triage", "Schedule", "Logistics", "Comms", "Coordinator")])
    talks = []
    in_room = []
    in_session = []
    about = []
    next_in = []
    presents = []
    for j, sess in enumerate(SESSIONS[:6]):
        for order in range(4):
            index = j * 4 + order
            tid = f"talk-{index+1:02d}"
            start = sess["start"] + 30 * order
            talks.append(dict(id=tid, title=TITLES[index], start=start, end=start + 25,
                              status="scheduled", budget=25, original_start=start,
                              subfield=sess["subfield"], junior=(index % 7 == 3)))
            in_room.append(dict(a=tid, b=sess["room"]))
            in_session.append(dict(a=tid, b=sess["id"], order=order + 1))
            about.append(dict(a=tid, b="topic-nlp" if tid == "talk-18" else f"topic-{sess['subfield'].lower()}"))
            speaker_index = index % 20
            if index == 16:
                speaker_index = 0  # keynote presenter has a later slot
            presents.append(dict(a=f"speaker-{speaker_index:02d}", b=tid))
            if order:
                next_in.append(dict(a=f"talk-{index:02d}", b=tid))
    batch(store, "Talk", talks)
    edge_batch(store, "Talk", "IN_ROOM", "Room", in_room)
    edge_batch(store, "Talk", "IN_SESSION", "Session", in_session, ["order"])
    edge_batch(store, "Talk", "ABOUT", "Topic", about)
    edge_batch(store, "Talk", "NEXT_IN", "Talk", next_in)
    people = [dict(id=f"speaker-{i:02d}", name=name, checked_in=i != 13,
                   role="speaker", career_stage="junior" if i % 7 == 3 else "senior")
              for i, name in enumerate(SPEAKERS)]
    people += [dict(id=f"attendee-{i:03d}", name=f"Attendee {i+1:03d}", checked_in=True,
                    role="attendee", career_stage="attendee") for i in range(300)]
    batch(store, "Person", people)
    edge_batch(store, "Person", "PRESENTS", "Talk", presents)
    edge_batch(store, "Person", "EXPERT_IN", "Topic", [dict(a=f"speaker-{i:02d}", b=f"topic-{TOPICS[i % 6].lower()}") for i in range(20)] + [dict(a="speaker-19", b="topic-nlp"), dict(a="speaker-13", b="topic-nlp")])
    edge_batch(store, "Person", "COAUTHORED", "Person", [dict(a="speaker-13", b="speaker-19"), dict(a="speaker-00", b="speaker-04")])
    edge_batch(store, "Person", "CHAIRS", "Session", [dict(a="speaker-01", b="session-nlp"), dict(a="speaker-12", b="session-nlp-panel"), dict(a="speaker-10", b="session-vision-panel")])
    rsvps = []
    for i in range(150):
        base = i % 4
        rsvps.append(dict(a=f"attendee-{i:03d}", b=f"talk-{base+1:02d}"))
        rsvps.append(dict(a=f"attendee-{i:03d}", b=f"talk-{base+5:02d}"))
        rsvps.append(dict(a=f"attendee-{i:03d}", b=f"talk-{13+(i % 4):02d}"))
        rsvps.append(dict(a=f"attendee-{i:03d}", b=f"talk-{17+(i % 4):02d}"))
        if i < 31:
            rsvps.append(dict(a=f"attendee-{i:03d}", b="talk-02"))
            rsvps.append(dict(a=f"attendee-{i:03d}", b="talk-07"))
    # de-duplicate RSVP pairs, while keeping the planted 31 cross-room conflicts.
    rsvps = [dict(a=a, b=b) for a, b in sorted({(r["a"], r["b"]) for r in rsvps})]
    edge_batch(store, "Person", "RSVP", "Talk", rsvps)
    events = [
        dict(id="event-lab-tour", name="Lab Tour", start=720, capacity=100, status="open"),
        dict(id="event-dinner", name="Welcome Dinner", start=1110, capacity=180, status="open"),
        dict(id="event-mixer", name="Research Mixer", start=1050, capacity=220, status="open"),
    ]
    batch(store, "SideEvent", events)
    edge_batch(store, "Person", "RSVP", "SideEvent", [dict(a=f"attendee-{i:03d}", b="event-lab-tour") for i in range(300)] + [dict(a=f"attendee-{i:03d}", b="event-dinner") for i in range(120)] + [dict(a=f"attendee-{i:03d}", b="event-mixer") for i in range(130)])
    batch(store, "Catering", [dict(id="food-lab-tour", item="boxed lunch", servings=100, time=720), dict(id="food-dinner", item="dinner", servings=180, time=1110), dict(id="food-mixer", item="snacks", servings=150, time=1050)])
    edge_batch(store, "Catering", "FOR", "SideEvent", [dict(a="food-lab-tour", b="event-lab-tour"), dict(a="food-dinner", b="event-dinner"), dict(a="food-mixer", b="event-mixer")])
    walks = []
    for a in ROOMS:
        for b in ROOMS:
            if a["id"] != b["id"]:
                cross = a["wing"] != b["wing"]
                walks.append(dict(a=a["id"], b=b["id"], mins=12 if cross else 4, stairs=cross,
                                  step_free_mins=16 if cross else 4))
    edge_batch(store, "Room", "WALK", "Room", walks, ["mins", "stairs", "step_free_mins"])
    edge_batch(store, "Person", "ASKED", "Talk", [dict(a="attendee-001", b=f"talk-{i:02d}", duration=3) for i in (1, 2, 3, 5, 6, 9, 10, 13, 14)], ["duration"])
    return {"seeded": True, "rooms": 4, "sessions": 7, "talks": 24, "speakers": 20,
            "attendees": 300, "side_events": 3, "talk_rsvps": len(rsvps), "seed": 1426}
