"""Five graph-sharing agents with deterministic safety rules and human approval."""
from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from itertools import combinations

from .db import Store
from .llm_agents import record_step


def clock(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def parse_incident(text: str) -> dict:
    """Fast local fallback; the optional OpenAI extractor uses the same schema."""
    s = text.lower()
    n = re.search(r"(\d+)\s*(?:minutes?|mins?)", s)
    minutes = int(n.group(1)) if n else 20
    talk_match = re.search(r"\btalk[- ]?(\d{1,2})\b", s)
    explicit_talk = f"talk-{int(talk_match.group(1)):02d}" if talk_match else None
    if any(x in s for x in ("power", "outage", "electricity")):
        return dict(type="power", wing="west" if "west" in s else "east", minutes=minutes)
    if any(x in s for x in ("no-show", "no show", "didn't show", "did not show", "absent")):
        return dict(type="no_show", talk_id=explicit_talk or "talk-14")
    if any(x in s for x in ("lab tour", "oversold", "catering", "food", "rsvp")):
        return dict(type="oversold", event_id="event-lab-tour")
    if any(x in s for x in ("pull forward", "pull-forward", "earlier", "gap opened")):
        return dict(type="pull_forward", talk_id=explicit_talk or "talk-15", minutes=int(n.group(1)) if n else 5)
    if "panel" in s and any(x in s for x in ("cancel", "called off")):
        return dict(type="panel_cancelled", session_id="session-nlp-panel")
    if "wrong track" in s or "misassigned" in s:
        return dict(type="misassignment", talk_id=explicit_talk or "talk-18")
    if "question" in s or "q&a" in s:
        return dict(type="qa_hog", person_id="attendee-001")
    if "panel" in s and any(x in s for x in ("over", "debt", "late")):
        return dict(type="panel_debt", talk_id=explicit_talk or "talk-13", minutes=minutes)
    if any(x in s for x in ("over", "late", "ran long")):
        return dict(type="overrun", talk_id=explicit_talk or "talk-01", minutes=minutes)
    raise ValueError("Describe an overrun, no-show, power outage, oversold event, panel issue, pull-forward, wrong track, or Q&A issue.")


def extract_with_openai(text: str) -> dict | None:
    """Optional tool call extracts only intent. All graph reads and decisions stay local."""
    import os
    if not os.getenv("OPENAI_API_KEY"):
        return None
    try:
        from openai import OpenAI
        response = OpenAI().chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
            messages=[{"role": "system", "content": "Extract the conference operations incident. Use the extract_incident tool. Defaults: keynote overrun talk-01; no-show talk-14; panel overrun talk-13; lab tour event-lab-tour; east wing power outage."},
                      {"role": "user", "content": text}],
            tools=[{"type": "function", "function": {"name": "extract_incident", "description": "Extract a structured conference incident", "parameters": {"type": "object", "properties": {"type": {"type": "string", "enum": ["overrun", "no_show", "oversold", "power", "pull_forward", "panel_debt", "panel_cancelled", "misassignment", "qa_hog"]}, "minutes": {"type": "integer"}, "talk_id": {"type": "string"}, "event_id": {"type": "string"}, "session_id": {"type": "string"}, "wing": {"type": "string"}, "person_id": {"type": "string"}}, "required": ["type"], "additionalProperties": False}}}],
            tool_choice={"type": "function", "function": {"name": "extract_incident"}},
            timeout=8,
        )
        return json.loads(response.choices[0].message.tool_calls[0].function.arguments)
    except Exception:
        return None


class Coordinator:
    def __init__(self, store: Store):
        self.store = store

    def talks(self):
        rows = self.store.read("MATCH (t:Talk)-[:IN_ROOM]->(r:Room) MATCH (t)-[:IN_SESSION]->(s:Session) RETURN t.id,t.title,t.start,t.end,t.budget,t.subfield,t.junior,r.id,r.capacity,s.id,s.start,s.end")
        return {r[0]: dict(id=r[0], title=r[1], start=r[2], end=r[3], budget=r[4],
                           subfield=r[5], junior=r[6], room=r[7], capacity=r[8],
                           session=r[9], session_start=r[10], session_end=r[11]) for r in rows}

    def validate(self, action: dict) -> str | None:
        kind = action["type"]
        if kind not in {"reschedule", "rehome", "pull_forward", "misassign", "overrun_recovery"}:
            return None
        talks = self.talks()
        rooms = {r[0]: (r[1], r[2]) for r in self.store.read("MATCH (r:Room) RETURN r.id,r.capacity,r.has_power")}
        changed = action.get("changes", [])
        for item in changed:
            t = talks.get(item["talk_id"])
            if not t:
                return "unknown talk"
            t.update(start=item.get("start", t["start"]), end=item.get("end", t["end"]),
                     room=item.get("room_id", t["room"]),
                     session=item.get("session_id", t["session"]),
                     subfield=item.get("subfield", t["subfield"]))
        moved = {x["talk_id"] for x in changed}
        for tid in moved:
            t = talks[tid]
            cap, powered = rooms.get(t["room"], (0, False))
            if not powered and kind != "rehome":
                return "room has no power"
            original_room_capacity = self.store.read("MATCH (t:Talk {id:$id})-[:IN_ROOM]->(r:Room) RETURN r.capacity", id=tid)[0][0]
            if cap < original_room_capacity and kind == "rehome":
                return "replacement room is too small"
            for oid, other in talks.items():
                if oid == tid or not (t["start"] < other["end"] and other["start"] < t["end"]):
                    continue
                if t["room"] == other["room"]:
                    return "two talks would occupy the same room"
                if t["subfield"] == other["subfield"] and t["session"] != other["session"]:
                    return f"two {t['subfield']} sessions would overlap"
            if t["junior"] and t["start"] >= 1020:
                return "junior presenter would be in the dead-last slot"
        # Existing room transitions cannot consume the mandatory break.
        for tid in moved:
            t = talks[tid]
            for other in talks.values():
                if other["id"] == tid or other["room"] != t["room"] or other["session"] == t["session"]:
                    continue
                gap = min(abs(t["start"] - other["end"]), abs(other["start"] - t["end"]))
                if not (t["start"] < other["end"] and other["start"] < t["end"]) and gap < 15:
                    return "break would be shorter than 15 minutes"
        # The final hour cannot have more concurrent sessions than the preceding hour.
        if any(talks[tid]["start"] >= 1020 for tid in moved):
            late = {t["session"] for t in talks.values() if t["start"] < 1065 and t["end"] > 1020}
            earlier = {t["session"] for t in talks.values() if t["start"] < 1020 and t["end"] > 975}
            if len(late) > len(earlier):
                return "too many concurrent sessions late in the day"
        new_clashes = self._new_rsvp_clashes(changed)
        if new_clashes:
            if kind != "overrun_recovery":
                return f"{len({p for p, _, _ in new_clashes})} attendees would have overlapping live RSVPs"
            fallbacks = {(item["person_id"], item["talk_id"]) for item in action.get("rsvp_fallbacks", [])}
            uncovered = [(person, a, b) for person, a, b in new_clashes
                         if (person, a) not in fallbacks and (person, b) not in fallbacks]
            if uncovered:
                return f"{len({p for p, _, _ in uncovered})} attendee conflicts lack a replay offer"
        return None

    def _new_rsvp_clashes(self, changes: list[dict]):
        original = self.talks()
        projected = {tid: talk.copy() for tid, talk in original.items()}
        moved = set()
        for item in changes:
            tid = item["talk_id"]
            if tid not in projected:
                continue
            projected[tid]["start"] = item.get("start", projected[tid]["start"])
            projected[tid]["end"] = item.get("end", projected[tid]["end"])
            if (projected[tid]["start"], projected[tid]["end"]) != (original[tid]["start"], original[tid]["end"]):
                moved.add(tid)
        if not moved:
            return []
        by_person = defaultdict(set)
        rows = self.store.read("MATCH (p:Person)-[r:RSVP]->(t:Talk) WHERE coalesce(r.status,'confirmed')='confirmed' RETURN p.id,t.id")
        for person, tid in rows:
            by_person[person].add(tid)
        clashes = []
        for person, ids in by_person.items():
            for a, b in combinations(sorted(ids), 2):
                if a not in moved and b not in moved:
                    continue
                before = original[a]["start"] < original[b]["end"] and original[b]["start"] < original[a]["end"]
                after = projected[a]["start"] < projected[b]["end"] and projected[b]["start"] < projected[a]["end"]
                if after and not before:
                    clashes.append((person, a, b))
        return clashes

    def propose(self, incident: str, agent: str, label: str, action: dict, path: list[str],
                confirm: bool = False, forced_rule: str | None = None):
        rule = forced_rule or self.validate(action)
        return self.store.fix(incident, agent, label, action, path, rule, confirm)

    def approve(self, fix_id: str):
        fix = self.store.get_fix(fix_id)
        if not fix:
            raise KeyError("fix not found")
        if fix["status"] != "proposed":
            raise ValueError("only proposed fixes can be approved")
        if fix["requires_confirmation"]:
            rows = self.store.read("MATCH (f:Fix {id:$id}) RETURN f.presenter_confirmed", id=fix_id)
            if not rows or not rows[0][0]:
                raise ValueError("presenter confirmation is required before an earlier move")
        rule = self.validate(fix["action"])
        if rule:
            self.store.q("MATCH (f:Fix {id:$id}) SET f.status='rejected', f.rejected_rule=$rule", id=fix_id, rule=rule)
            raise ValueError(rule)
        watch_walk = fix["action"]["type"] in {"reschedule", "rehome", "pull_forward", "misassign", "overrun_recovery"}
        before_walk = self._walk_violations() if watch_walk else {}
        self._apply(fix["action"])
        if fix["action"]["type"] == "overrun_recovery":
            self.store.q("MATCH (f:Fix {id:$fix})-[:RESOLVES]->(root:Incident) SET root.status='resolved'", fix=fix_id)
            self.store.q("MATCH (f:Fix {id:$fix})-[:RESOLVES]->(root:Incident)-[:CAUSED]->(child:Incident {type:'rsvp_clash'}) SET child.status='resolved'", fix=fix_id)
        if watch_walk:
            self._record_new_walk_issues(fix["incident_id"], fix_id, before_walk)
        self.store.q("MATCH (f:Fix {id:$id}) SET f.status='approved'", id=fix_id)
        return self.store.get_fix(fix_id)

    def _walk_violations(self):
        rows = self.store.read("""
            MATCH (p:Person)-[r1:RSVP]->(a:Talk)-[:IN_ROOM]->(ra:Room),
                  (p)-[r2:RSVP]->(b:Talk)-[:IN_ROOM]->(rb:Room),
                  (ra)-[w:WALK]->(rb)
            WHERE coalesce(r1.status,'confirmed')='confirmed' AND coalesce(r2.status,'confirmed')='confirmed'
              AND b.start >= a.end AND b.start-a.end < w.mins
            RETURN DISTINCT p.id,a.id,b.id,w.mins,b.start-a.end,w.step_free_mins
        """)
        return {(r[0], r[1], r[2]): r[3:] for r in rows}

    def _record_new_walk_issues(self, incident_id: str, fix_id: str, before: dict):
        after = self._walk_violations()
        drafts = []
        for (pid, from_talk, to_talk), (required, gap, step_free) in after.items():
            if (pid, from_talk, to_talk) in before:
                continue
            child = self.store.incident("walk_time", f"{pid} needs {required} minutes to walk from {from_talk} to {to_talk}, but has {gap}; step-free route needs {step_free}", "Coordinator", incident_id)
            self.store.affects(child, "Person", pid)
            self.store.affects(child, "Talk", to_talk)
            drafts.append(dict(person_id=pid, subject="Transfer time after schedule update",
                               body=f"Your transfer from {from_talk} to {to_talk} has only {gap} minutes; the usual route takes {required} minutes and the step-free route takes {step_free}. This draft needs an organizer update."))
        self.store.notifications_bulk(incident_id, fix_id, drafts)

    def confirm(self, fix_id: str, person_id: str):
        fix = self.store.get_fix(fix_id)
        if not fix or fix["action"]["type"] != "pull_forward":
            raise ValueError("this fix does not need presenter confirmation")
        tid = fix["action"]["changes"][0]["talk_id"]
        match = self.store.read("MATCH (p:Person {id:$pid})-[:PRESENTS]->(t:Talk {id:$tid}) RETURN p.id", pid=person_id, tid=tid)
        if not match:
            raise ValueError("only this talk's presenter can confirm")
        self.store.q("MATCH (f:Fix {id:$id}) SET f.presenter_confirmed=true", id=fix_id)
        return self.store.get_fix(fix_id)

    def _apply(self, action: dict):
        kind = action["type"]
        if kind in {"reschedule", "rehome", "pull_forward", "misassign", "overrun_recovery"}:
            for change in action["changes"]:
                self.store.q("MATCH (t:Talk {id:$id}) SET t.start=$start,t.end=$end", id=change["talk_id"], start=change["start"], end=change["end"])
                if "room_id" in change:
                    self.store.q("MATCH (t:Talk {id:$tid})-[old:IN_ROOM]->(:Room), (r:Room {id:$rid}) DELETE old CREATE (t)-[:IN_ROOM]->(r)", tid=change["talk_id"], rid=change["room_id"])
                if "session_id" in change:
                    self.store.q("MATCH (t:Talk {id:$tid})-[old:IN_SESSION]->(:Session), (s:Session {id:$sid}) DELETE old CREATE (t)-[:IN_SESSION {order:99}]->(s)", tid=change["talk_id"], sid=change["session_id"])
                if "subfield" in change:
                    self.store.q("MATCH (t:Talk {id:$tid}) SET t.subfield=$subfield", tid=change["talk_id"], subfield=change["subfield"])
                if kind == "misassign":
                    self.store.q("MATCH (t:Talk {id:$tid}) SET t.status='scheduled'", tid=change["talk_id"])
            self._rebuild_order()
            for sid, end in self.store.read("MATCH (t:Talk)-[:IN_SESSION]->(s:Session) RETURN s.id,max(t.end)"):
                self.store.q("MATCH (s:Session {id:$id}) WHERE s.end < $end SET s.end=$end", id=sid, end=end)
            if kind == "overrun_recovery" and action.get("rsvp_fallbacks"):
                self.store.q("""
                    UNWIND $rows AS row
                    MATCH (p:Person {id:row.person_id})-[r:RSVP]->(t:Talk {id:row.talk_id})
                    SET r.status='replay_offered'
                """, rows=action["rsvp_fallbacks"])
        elif kind == "substitute":
            self.store.q("MATCH (orig:Person {id:$orig})-[old:PRESENTS]->(t:Talk {id:$tid}), (sub:Person {id:$sub}) DELETE old CREATE (sub)-[:PRESENTS]->(t) CREATE (sub)-[:SUBSTITUTED_FOR]->(orig) SET t.status='substituted'", orig=action["original_person"], tid=action["talk_id"], sub=action["replacement_person"])
        elif kind == "waitlist":
            event = action["event_id"]
            rows = self.store.read("MATCH (p:Person)-[r:RSVP]->(e:SideEvent {id:$id}) RETURN p.id ORDER BY p.id", id=event)
            confirmed = [r[0] for r in rows[:action["capacity"]]]
            waitlisted = [r[0] for r in rows[action["capacity"]:]]
            self.store.q("UNWIND $people AS pid MATCH (p:Person {id:pid})-[r:RSVP]->(e:SideEvent {id:$event}) SET r.status='confirmed'", people=confirmed, event=event)
            self.store.q("UNWIND $people AS pid MATCH (p:Person {id:pid})-[r:RSVP]->(e:SideEvent {id:$event}) SET r.status='waitlisted'", people=waitlisted, event=event)
        elif kind == "panel_budget":
            for item in action["budgets"]:
                self.store.q("MATCH (t:Talk {id:$id}) SET t.budget=$budget", id=item["talk_id"], budget=item["budget"])
        elif kind == "cancel_panel":
            self.store.q("MATCH (s:Session {id:$id}) SET s.status='cancelled'", id=action["session_id"])
            self.store.q("MATCH (t:Talk)-[:IN_SESSION]->(s:Session {id:$id}) SET t.status='cancelled'", id=action["session_id"])
        elif kind == "pad_speaker":
            self.store.q("MATCH (t:Talk {id:$id}) SET t.flagged=true,t.padding=$padding", id=action["talk_id"], padding=action["minutes"])
        if kind == "rehome" and action.get("wing"):
            self.store.q("MATCH (r:Room {wing:$wing}) SET r.has_power=false", wing=action["wing"])

    def _rebuild_order(self):
        self.store.q("MATCH (:Talk)-[r:NEXT_IN]->(:Talk) DELETE r")
        rows = self.store.read("MATCH (t:Talk)-[:IN_ROOM]->(r:Room) MATCH (t)-[:IN_SESSION]->(s:Session) RETURN t.id,r.id,s.id,t.start ORDER BY r.id,s.id,t.start")
        grouped = defaultdict(list)
        for tid, room, session, _ in rows:
            grouped[(room, session)].append(tid)
        links = [dict(a=a, b=b) for ids in grouped.values() for a, b in zip(ids, ids[1:])]
        if links:
            self.store.q("UNWIND $links AS row MATCH (a:Talk {id:row.a}), (b:Talk {id:row.b}) CREATE (a)-[:NEXT_IN]->(b)", links=links)


class Agents:
    def __init__(self, store: Store):
        self.store = store
        self.coordinator = Coordinator(store)

    def run(self, text: str, details: dict | None = None):
        data = details or extract_with_openai(text) or parse_incident(text)
        # Tool output can contain a natural-language name where a graph ID is required.
        # Keep the LLM as an extractor, and resolve every target against shared graph state.
        fallback = parse_incident(text) if details is None else {}
        labels = {"talk_id": "Talk", "event_id": "SideEvent", "session_id": "Session", "person_id": "Person"}
        for key, label in labels.items():
            if key in data:
                rows = self.store.read(f"MATCH (n:{label} {{id:$id}}) RETURN n.id LIMIT 1", id=str(data[key]))
                if not rows:
                    if details is not None:
                        raise ValueError(f"unknown {key}: {data[key]}")
                    data[key] = fallback.get(key)
            elif key in fallback:
                data[key] = fallback[key]
        if data.get("wing") not in {None, "east", "west"}:
            if details is not None:
                raise ValueError("wing must be east or west")
            data["wing"] = fallback.get("wing", "east")
        if "minutes" in data:
            data["minutes"] = max(1, min(int(data["minutes"]), 180))
        kind = data["type"]
        incident = self.store.incident(kind, text)
        if kind in {"overrun", "panel_debt", "no_show", "pull_forward", "misassignment"}:
            self.store.affects(incident, "Talk", data.get("talk_id", "talk-01"))
        elif kind == "power":
            for row in self.store.read("MATCH (r:Room {wing:$wing}) RETURN r.id", wing=data.get("wing", "east")):
                self.store.affects(incident, "Room", row[0])
        elif kind == "oversold":
            self.store.affects(incident, "SideEvent", data.get("event_id", "event-lab-tour"))
        elif kind == "panel_cancelled":
            self.store.affects(incident, "Session", data.get("session_id", "session-nlp-panel"))
        elif kind == "qa_hog":
            self.store.affects(incident, "Person", data.get("person_id", "attendee-001"))
        record_step(self.store, "Triage", incident, f"Classified the report as {kind} and linked affected graph nodes.")

        # Schedule writes proposals and child incidents. Logistics subsequently reads those writes.
        if kind == "overrun":
            self._overrun(incident, data)
        elif kind == "no_show":
            self._no_show(incident, data)
        elif kind == "power":
            self._power(incident, data)
        elif kind == "pull_forward":
            self._pull_forward(incident, data)
        elif kind == "panel_debt":
            self._panel_debt(incident, data)
        elif kind == "panel_cancelled":
            self._panel_cancelled(incident, data)
        elif kind == "misassignment":
            self._misassignment(incident, data)
        elif kind == "qa_hog":
            self._qa_hog(incident, data)
        schedule_fixes = [f for f in self.store.fixes() if f["incident_id"] == incident]
        record_step(self.store, "Schedule", incident, f"Checked graph paths and wrote {len(schedule_fixes)} schedule options and any cascade incidents.")
        self._logistics(incident, data)
        child_count = self.store.read("MATCH (i:Incident {id:$id})-[:CAUSED]->(child:Incident) RETURN count(child)", id=incident)[0][0]
        record_step(self.store, "Logistics", incident, f"Checked the shared graph for capacity and conflicts; {child_count} child incidents are recorded.")
        self._comms(incident)
        draft_count = self.store.read("MATCH (i:Incident {id:$id})-[:DRAFTED]->(n:Notification) RETURN count(n)", id=incident)[0][0]
        record_step(self.store, "Comms", incident, f"Drafted {draft_count} notices for people reached by the proposed changes.")
        final_fixes = [f for f in self.store.fixes() if f["incident_id"] == incident]
        proposed_count = sum(f["status"] == "proposed" for f in final_fixes)
        rejected_count = sum(f["status"] == "rejected" for f in final_fixes)
        record_step(self.store, "Coordinator", incident, f"Rule checks left {proposed_count} options for human approval and rejected {rejected_count}.")
        return {"incident_id": incident, "type": kind,
                "fixes": [f for f in self.store.fixes() if f["incident_id"] == incident],
                "notifications": [n for n in self.store.notifications() if n["incident_id"] == incident]}

    def _overrun(self, incident: str, data: dict):
        tid, mins = data.get("talk_id", "talk-01"), int(data.get("minutes", 20))
        rows = self.store.read("MATCH (t:Talk {id:$id})-[:NEXT_IN*1..]->(d:Talk) RETURN DISTINCT d.id,d.title,d.start,d.end ORDER BY d.start", id=tid)
        origin = self.store.read("MATCH (t:Talk {id:$id}) RETURN t.title,t.start,t.end", id=tid)
        if not origin:
            return
        changes = [dict(talk_id=tid, start=origin[0][1], end=origin[0][2] + mins)]
        changes += [dict(talk_id=r[0], start=r[2] + mins, end=r[3] + mins) for r in rows]
        if not changes:
            return
        new_clashes = self.coordinator._new_rsvp_clashes(changes)
        moved = {item["talk_id"] for item in changes}
        fallbacks = set()
        for person, a, b in new_clashes:
            if a in moved and b not in moved:
                fallback_talk = b
            elif b in moved and a not in moved:
                fallback_talk = a
            else:
                fallback_talk = b
            fallbacks.add((person, fallback_talk))
        fallback_rows = [dict(person_id=person, talk_id=talk) for person, talk in sorted(fallbacks)]
        attendees = len({person for person, _, _ in new_clashes})
        path = [f"Talk:{tid}", "NEXT_IN*", *[f"Talk:{r[0]}" for r in rows],
                f"Person:{attendees} affected attendees", "RSVP", "Talk:replay offers"]
        action = dict(type="overrun_recovery", changes=changes, rsvp_fallbacks=fallback_rows,
                      overrun_talk=tid, minutes=mins)
        label = f"Shift {len(changes)-1} talks and offer replay for {attendees} conflicting attendees"
        self.coordinator.propose(incident, "Schedule", label, action, path)
        if attendees:
            unsafe = dict(type="reschedule", changes=changes, overrun_talk=tid, minutes=mins)
            self.coordinator.propose(incident, "Schedule", f"Shift talks without handling {attendees} attendee conflicts", unsafe,
                                     [f"Talk:{tid}", "NEXT_IN*", "RSVP", f"Person:{attendees} attendees"])
        # A tempting alternative visibly fails the subfield guard.
        if tid == "talk-01":
            rejected = dict(type="reschedule", changes=[dict(talk_id="talk-14", start=570, end=595)])
            self.coordinator.propose(incident, "Schedule", "Move the NLP panel talk into the morning NLP block", rejected,
                                     ["Talk:talk-14", "Session:session-nlp", "Talk:talk-06"],
                                     forced_rule="two NLP sessions would overlap")
        # Projected child incidents show the risk; approval handles each with a replay offer.
        for change in changes[1:]:
            self.store.affects(incident, "Talk", change["talk_id"])
        for person, a, b in new_clashes:
            child = self.store.incident("rsvp_clash", f"{person} would have overlapping live RSVPs: {a} and {b}",
                                        "Schedule", incident, status="projected")
            self.store.affects(child, "Person", person)
            self.store.affects(child, "Talk", a)
            self.store.affects(child, "Talk", b)
        self._repeat_offender(incident, tid)

    def _repeat_offender(self, incident: str, tid: str):
        rows = self.store.read("MATCH (p:Person)-[:PRESENTS]->(t:Talk {id:$id}) RETURN p.id", id=tid)
        if not rows:
            return
        pid = rows[0][0]
        count = self.store.read("MATCH (p:Person {id:$pid})-[:PRESENTS]->(:Talk)<-[:AFFECTS]-(i:Incident {type:'overrun'}) RETURN count(i)", pid=pid)[0][0]
        if count < 2:
            return
        next_rows = self.store.read("MATCH (p:Person {id:$pid})-[:PRESENTS]->(t:Talk) WHERE t.start > $start RETURN t.id,t.title ORDER BY t.start LIMIT 1", pid=pid, start=self.store.read("MATCH (t:Talk {id:$id}) RETURN t.start", id=tid)[0][0])
        if next_rows:
            target = next_rows[0][0]
            self.coordinator.propose(incident, "Schedule", f"Pre-flag {next_rows[0][1]}: presenter has {count} overruns", dict(type="pad_speaker", talk_id=target, minutes=5), [f"Person:{pid}", "PRESENTS", f"Talk:{target}"])

    def _no_show(self, incident: str, data: dict):
        tid = data.get("talk_id", "talk-14")
        rows = self.store.read("MATCH (orig:Person)-[:PRESENTS]->(t:Talk {id:$id})-[:ABOUT]->(tp:Topic)<-[:EXPERT_IN]-(sub:Person) WHERE sub.checked_in=true AND sub.id <> orig.id RETURN orig.id,orig.name,sub.id,sub.name,tp.name,t.start,t.end", id=tid)
        candidates = []
        for orig, oname, sub, sname, topic, start, end in rows:
            busy = self.store.read("MATCH (p:Person {id:$id})-[:PRESENTS]->(x:Talk) WHERE x.start < $end AND x.end > $start RETURN x.id LIMIT 1", id=sub, start=start, end=end)
            if busy:
                continue
            coauthor = bool(self.store.read("MATCH (a:Person {id:$a})-[:COAUTHORED]-(b:Person {id:$b}) RETURN a.id LIMIT 1", a=orig, b=sub))
            candidates.append((not coauthor, sub, sname, orig, oname, topic))
        for _, sub, sname, orig, oname, topic in sorted(candidates)[:1]:
            self.store.affects(incident, "Person", orig)
            self.store.affects(incident, "Person", sub)
            self.coordinator.propose(incident, "Schedule", f"Substitute {sname} for {oname} on {topic}",
                dict(type="substitute", talk_id=tid, original_person=orig, replacement_person=sub),
                [f"Person:{orig}", "COAUTHORED" if _ is False else "EXPERT_IN", f"Person:{sub}", f"Topic:{topic}", f"Talk:{tid}"])

    def _power(self, incident: str, data: dict):
        wing = data.get("wing", "east")
        rows = self.store.read("MATCH (t:Talk)-[:IN_ROOM]->(r:Room {wing:$wing}) RETURN t.id,t.title,t.start,t.end,r.id,r.capacity ORDER BY t.start", wing=wing)
        alternatives = self.store.read("MATCH (r:Room) WHERE r.wing <> $wing AND r.has_power=true RETURN r.id,r.name,r.capacity ORDER BY r.capacity DESC", wing=wing)
        all_talks = self.coordinator.talks()
        changes = []
        for tid, title, start, end, room, capacity in rows:
            self.store.affects(incident, "Talk", tid)
            chosen = None
            for alt_id, _, alt_cap in alternatives:
                if alt_cap < capacity:
                    continue
                occupied = any(x["room"] == alt_id and x["start"] < end and x["end"] > start for x in all_talks.values())
                if not occupied:
                    chosen = alt_id
                    break
            if chosen:
                changes.append(dict(talk_id=tid, start=start, end=end, room_id=chosen))
                all_talks[tid]["room"] = chosen
            else:
                child = self.store.incident("unplaced_talk", f"No powered room fits {title} at {clock(start)}", "Schedule", incident)
                self.store.affects(child, "Talk", tid)
        if changes:
            self.coordinator.propose(incident, "Schedule", f"Re-home {len(changes)} talks out of the {wing} wing", dict(type="rehome", changes=changes, wing=wing),
                [f"Room:{wing} wing", "IN_ROOM", *[f"Talk:{x['talk_id']}" for x in changes], "Room:powered alternatives"])

    def _pull_forward(self, incident: str, data: dict):
        tid, mins = data.get("talk_id", "talk-15"), int(data.get("minutes", 20))
        row = self.store.read("MATCH (t:Talk {id:$id}) RETURN t.title,t.start,t.end", id=tid)
        if not row:
            return
        title, start, end = row[0]
        action = dict(type="pull_forward", changes=[dict(talk_id=tid, start=start-mins, end=end-mins)])
        self.coordinator.propose(incident, "Schedule", f"Move {title} {mins} minutes earlier, pending presenter confirmation", action,
                                 [f"Talk:{tid}", "PRESENTS", "Person:presenter", "RSVP", "Person:attendees"], confirm=True)

    def _panel_debt(self, incident: str, data: dict):
        tid, mins = data.get("talk_id", "talk-13"), int(data.get("minutes", 20))
        row = self.store.read("MATCH (t:Talk {id:$id})-[:IN_SESSION]->(s:Session) RETURN t.start,t.end,s.id,s.end", id=tid)
        if not row:
            return
        start, end, sid, session_end = row[0]
        remaining = self.store.read("MATCH (t:Talk)-[rel:IN_SESSION]->(s:Session {id:$id}) WHERE t.start > $start RETURN t.id,t.title,rel.order ORDER BY rel.order", id=sid, start=start)
        if not remaining:
            return
        available = max(0, session_end - (end + mins))
        budget = available // len(remaining)
        budgets = [dict(talk_id=r[0], budget=min(25, budget)) for r in remaining]
        self.coordinator.propose(incident, "Schedule", f"Reallocate panel time: {len(remaining)} speakers get {budgets[0]['budget']} min each; chair chooses", dict(type="panel_budget", budgets=budgets),
                                 [f"Talk:{tid}", f"Session:{sid}", *[f"Talk:{r[0]}" for r in remaining]])
        if session_end + mins > 945:
            self.coordinator.propose(incident, "Schedule", "Extend panel into the scheduled break", dict(type="panel_budget", budgets=budgets),
                                     [f"Session:{sid}", "break"], forced_rule="break would be shorter than 15 minutes")

    def _panel_cancelled(self, incident: str, data: dict):
        sid = data.get("session_id", "session-nlp-panel")
        self.coordinator.propose(incident, "Schedule", "Cancel the panel and notify presenters and RSVPs", dict(type="cancel_panel", session_id=sid), [f"Session:{sid}", "IN_SESSION", "PRESENTS", "RSVP"])
        if sid == "session-nlp-panel":
            orphaned = self.store.read("MATCH (t:Talk)-[rel:IN_SESSION]->(s:Session {id:$id}) RETURN t.id,t.title,rel.order ORDER BY rel.order", id=sid)
            for index, (tid, title, _) in enumerate(orphaned):
                if index >= 2:
                    self.coordinator.propose(incident, "Schedule", f"Find a related slot for {title}", dict(type="misassign", changes=[]),
                                             [f"Talk:{tid}", "ABOUT", "Session:session-nlp-overflow"], forced_rule="related session has no open future slot")
                    continue
                start = 960 + index * 30
                action = dict(type="misassign", changes=[dict(talk_id=tid, start=start, end=start+25,
                                                               room_id="room-west-lab", session_id="session-nlp-overflow", subfield="NLP")])
                self.coordinator.propose(incident, "Schedule", f"Offer {title} a {clock(start)} slot in NLP Late-Breaking Talks", action,
                                         [f"Talk:{tid}", "ABOUT", "Session:session-nlp-overflow", "Room:room-west-lab"])

    def _misassignment(self, incident: str, data: dict):
        tid = data.get("talk_id", "talk-14")
        rows = self.store.read("MATCH (t:Talk {id:$id})-[:ABOUT]->(tp:Topic), (s:Session) WHERE s.subfield=tp.name AND NOT (t)-[:IN_SESSION]->(s) RETURN s.id,s.title,s.start,s.end ORDER BY s.start DESC", id=tid)
        talk = self.store.read("MATCH (t:Talk {id:$id}) RETURN t.start,t.end", id=tid)
        if not talk:
            return
        duration = talk[0][1] - talk[0][0]
        for sid, title, start, end in rows:
            existing = self.store.read("MATCH (t:Talk)-[:IN_SESSION]->(s:Session {id:$id}) RETURN t.start,t.end ORDER BY t.start", id=sid)
            slots = [start] + [r[1] + 5 for r in existing]
            for candidate in slots:
                if candidate < talk[0][0]:
                    continue
                if candidate + duration > end or any(candidate < r[1] and r[0] < candidate + duration for r in existing):
                    continue
                room = self.store.read("MATCH (s:Session {id:$id})-[:IN_ROOM]->(r:Room) RETURN r.id LIMIT 1", id=sid)
                if not room:
                    continue
                action = dict(type="misassign", changes=[dict(talk_id=tid, start=candidate, end=candidate+duration, room_id=room[0][0], session_id=sid, subfield="NLP")])
                self.coordinator.propose(incident, "Schedule", f"Move talk into {title}", action, [f"Talk:{tid}", "ABOUT", f"Session:{sid}"])
                return
        self.coordinator.propose(incident, "Schedule", "Move talk to a matching session", dict(type="misassign", changes=[]), [f"Talk:{tid}", "ABOUT", "Session:matching"], forced_rule="no matching session has an open slot")

    def _qa_hog(self, incident: str, data: dict):
        pid = data.get("person_id", "attendee-001")
        rows = self.store.read("MATCH (p:Person {id:$id})-[r:ASKED]->(t:Talk)-[:IN_SESSION]->(s:Session) RETURN p.name,count(r),count(DISTINCT s),avg(r.duration)", id=pid)
        if rows and rows[0][1]:
            name, questions, sessions, average = rows[0]
            self.coordinator.propose(incident, "Logistics", f"Chair heads-up: {name} asked {questions} questions across {sessions} sessions, average {average:.1f} min; invite other questioners first", dict(type="advisory"), [f"Person:{pid}", "ASKED", "Talk:history"])

    def _logistics(self, incident: str, data: dict):
        # Read Triage's written incident and Schedule's child incidents, then add capacity work.
        rows = self.store.read("MATCH (i:Incident {id:$id}) RETURN i.type", id=incident)
        if not rows:
            return
        if rows[0][0] == "oversold":
            event = data.get("event_id", "event-lab-tour")
            row = self.store.read("MATCH (e:SideEvent {id:$id}) OPTIONAL MATCH (p:Person)-[r:RSVP]->(e) OPTIONAL MATCH (c:Catering)-[:FOR]->(e) RETURN e.name,e.capacity,count(DISTINCT p),sum(DISTINCT c.servings)", id=event)
            if row:
                name, capacity, count, servings = row[0]
                servings = servings or 0
                if count > capacity or servings < count:
                    self.coordinator.propose(incident, "Logistics", f"Waitlist {max(0,count-capacity)} for {name}; confirm {capacity} places and {servings} meals", dict(type="waitlist", event_id=event, capacity=capacity),
                        [f"SideEvent:{event}", "RSVP", f"Person:{count} RSVPs", "Catering:food-lab-tour"])
                    if servings < count:
                        child = self.store.incident("catering_gap", f"{name} has {count} RSVPs but only {servings} servings", "Logistics", incident)
                        self.store.affects(child, "SideEvent", event)
                        self.store.affects(child, "Catering", "food-lab-tour")
        # Projected overrun clashes are handled by the integrated recovery plan.

    def _comms(self, incident: str):
        fixes = [f for f in self.store.fixes() if f["incident_id"] == incident and f["status"] == "proposed"]
        for fix in fixes:
            action = fix["action"]
            kind = action["type"]
            if kind == "advisory":
                continue
            people: dict[str, str] = {}
            talk_ids = []
            if kind in {"reschedule", "rehome", "pull_forward", "misassign", "overrun_recovery"}:
                talk_ids = [c["talk_id"] for c in action.get("changes", [])]
            elif kind == "substitute":
                talk_ids = [action["talk_id"]]
            elif kind == "panel_budget":
                talk_ids = [b["talk_id"] for b in action["budgets"]]
            elif kind == "pad_speaker":
                talk_ids = [action["talk_id"]]
            if talk_ids:
                relation = "PRESENTS" if kind == "pad_speaker" else "RSVP|PRESENTS"
                rows = self.store.read(f"UNWIND $ids AS id MATCH (p:Person)-[:{relation}]->(t:Talk {{id:id}}) RETURN DISTINCT p.id,p.name", ids=talk_ids)
                people.update(rows)
            if kind == "substitute":
                rows = self.store.read("MATCH (p:Person {id:$id}) RETURN p.id,p.name", id=action["replacement_person"])
                people.update(rows)
            if kind == "waitlist":
                rows = self.store.read("MATCH (p:Person)-[:RSVP]->(e:SideEvent {id:$id}) RETURN DISTINCT p.id,p.name", id=action["event_id"])
                people.update(rows)
            if kind == "cancel_panel":
                rows = self.store.read("MATCH (t:Talk)-[:IN_SESSION]->(s:Session {id:$id}) MATCH (p:Person)-[:RSVP|PRESENTS]->(t) RETURN DISTINCT p.id,p.name", id=action["session_id"])
                people.update(rows)
            subject = f"Proposed conference update: {fix['proposal']}"
            drafts = []
            for pid, name in people.items():
                body = f"Hi {name}, a conference incident may affect your session or event. Proposed update: {fix['proposal']}. This is a draft until the organizer approves the fix."
                if fix["requires_confirmation"]:
                    body += " The presenter must confirm before an earlier start time can be applied."
                if kind == "rehome" and pid.startswith("speaker-"):
                    relevant = self.store.read("MATCH (p:Person {id:$pid})-[:PRESENTS]->(t:Talk) RETURN t.id", pid=pid)
                    move = next((c for c in action["changes"] if c["talk_id"] in {r[0] for r in relevant}), None)
                    if move:
                        room = self.store.read("MATCH (r:Room {id:$id}) RETURN r.name", id=move["room_id"])[0][0]
                        count = self.store.read("MATCH (p:Person)-[:RSVP]->(t:Talk {id:$id}) RETURN count(p)", id=move["talk_id"])[0][0]
                        body += f" Room briefing: {room}; {count} RSVP'd attendees; check the updated room map before presenting."
                if kind == "waitlist":
                    body += " You will be confirmed or waitlisted in RSVP order; catering covers the first 100 confirmed guests."
                if kind == "overrun_recovery":
                    offered = [r["talk_id"] for r in action["rsvp_fallbacks"] if r["person_id"] == pid]
                    if offered:
                        body += f" Your conflicting live RSVP for {', '.join(offered)} will become a replay offer; the other RSVP stays live."
                drafts.append(dict(person_id=pid, subject=subject, body=body))
            self.store.notifications_bulk(incident, fix["id"], drafts)


def preflight(store: Store):
    overlapping = store.read("MATCH (a:Talk)-[:ABOUT]->(tp:Topic)<-[:ABOUT]-(b:Talk) WHERE a.id < b.id AND a.start < b.end AND b.start < a.end RETURN a.id,b.id,tp.name")
    talks = Coordinator(store).talks()
    same_topic = [(talks[a]["title"], talks[b]["title"], topic) for a, b, topic in overlapping
                  if talks[a]["session"] != talks[b]["session"]]
    chair = store.read("MATCH (p:Person)-[:CHAIRS]->(s:Session)<-[:IN_SESSION]-(t:Talk)<-[:PRESENTS]-(p) RETURN DISTINCT p.name,s.title")
    walk = store.read("MATCH (p:Person)-[r1:RSVP]->(a:Talk)-[:IN_ROOM]->(ra:Room), (p)-[r2:RSVP]->(b:Talk)-[:IN_ROOM]->(rb:Room), (ra)-[w:WALK]->(rb) WHERE coalesce(r1.status,'confirmed')='confirmed' AND coalesce(r2.status,'confirmed')='confirmed' AND b.start >= a.end AND b.start-a.end < w.mins RETURN DISTINCT p.name,a.title,b.title,w.mins,b.start-a.end,w.step_free_mins LIMIT 100")
    qa = store.read("MATCH (p:Person)-[r:ASKED]->(t:Talk) RETURN p.name,count(r),avg(r.duration) ORDER BY count(r) DESC LIMIT 5")
    return {"audience_cannibalization": [dict(a=r[0], b=r[1], topic=r[2]) for r in same_topic],
            "chair_conflicts": [dict(person=r[0], session=r[1]) for r in chair],
            "walk_time": [dict(person=r[0], from_talk=r[1], to_talk=r[2], required=r[3], gap=r[4], step_free=r[5]) for r in walk],
            "qa_hogs": [dict(person=r[0], questions=r[1], avg_minutes=round(r[2],1)) for r in qa]}
