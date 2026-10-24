"""Integration tests run against an isolated FalkorDB graph."""
import pytest

from app.db import Store
from app.engine import Agents, Coordinator, preflight
from app.seed import seed


@pytest.fixture(scope="module")
def demo():
    store = Store("blast_radius_test")
    seed(store, reset=True)
    try:
        yield store, Agents(store), Coordinator(store)
    finally:
        store.graph.delete()


def test_seed_and_preflight(demo):
    store, _, _ = demo
    assert store.read("MATCH (t:Talk) RETURN count(t)")[0][0] == 24
    assert store.read("MATCH (s:Session) RETURN count(s)")[0][0] == 7
    assert store.read("MATCH (p:Person {role:'attendee'}) RETURN count(p)")[0][0] == 300
    findings = preflight(store)
    assert findings["chair_conflicts"]
    assert len(findings["audience_cannibalization"]) == 1
    assert findings["walk_time"]
    assert findings["qa_hogs"][0]["questions"] == 9


def test_approval_and_shared_state(demo):
    store, agents, coordinator = demo
    original = store.read("MATCH (t:Talk {id:'talk-02'}) RETURN t.start")[0][0]
    result = agents.run("Keynote running 20 minutes over")
    assert store.read("MATCH (a:Agent)-[:RAN]->(run:AgentRun)-[:REVIEWS]->(i:Incident {id:$id}) RETURN count(run)", id=result["incident_id"])[0][0] == 5
    proposals = [f for f in result["fixes"] if f["status"] == "proposed"]
    assert proposals
    assert any(f["rejected_rule"] == "two NLP sessions would overlap" for f in result["fixes"])
    assert any(f["rejected_rule"] == "31 attendees would have overlapping live RSVPs" for f in result["fixes"])
    assert store.read("MATCH (t:Talk {id:'talk-02'}) RETURN t.start")[0][0] == original
    assert store.read("MATCH (i:Incident {id:$id})-[:CAUSED]->(child:Incident {type:'rsvp_clash'}) RETURN count(child)", id=result["incident_id"])[0][0] == 31
    recovery = next(f for f in proposals if f["action"]["type"] == "overrun_recovery")
    assert len(recovery["action"]["rsvp_fallbacks"]) == 31
    coordinator.approve(recovery["id"])
    assert store.read("MATCH (t:Talk {id:'talk-02'}) RETURN t.start")[0][0] == original + 20
    assert store.read("MATCH (s:Session {id:'session-keynote'}) RETURN s.end")[0][0] == 675
    assert store.read("MATCH (:Person)-[r:RSVP {status:'replay_offered'}]->(:Talk) RETURN count(r)")[0][0] == 31
    assert store.read("MATCH (i:Incident {id:$id}) RETURN i.status", id=result["incident_id"])[0][0] == "resolved"
    assert store.read("MATCH (i:Incident {id:$id})-[:CAUSED]->(child:Incident {type:'rsvp_clash', status:'resolved'}) RETURN count(child)", id=result["incident_id"])[0][0] == 31
    assert len([f for f in store.fixes() if f["id"] == recovery["id"]]) == 1

    absent = agents.run("Dr. Mira Solis didn't show")
    substitute = next(f for f in absent["fixes"] if f["action"]["type"] == "substitute")
    assert substitute["action"]["replacement_person"] == "speaker-19"
    coordinator.approve(substitute["id"])
    assert store.read("MATCH (p:Person {id:'speaker-19'})-[:PRESENTS]->(t:Talk {id:'talk-14'}) RETURN p.id")

    oversold = agents.run("Lab tour just opened to 300 RSVPs")
    assert store.read("MATCH (i:Incident {id:$id})-[:CAUSED]->(c:Incident {type:'catering_gap'}) RETURN count(c)", id=oversold["incident_id"])[0][0] == 1
    waitlist = next(f for f in oversold["fixes"] if f["action"]["type"] == "waitlist")
    coordinator.approve(waitlist["id"])
    assert store.read("MATCH (:Person)-[r:RSVP {status:'confirmed'}]->(:SideEvent {id:'event-lab-tour'}) RETURN count(r)")[0][0] == 100

    again = agents.run("Keynote running 5 minutes over")
    assert any(f["action"]["type"] == "pad_speaker" for f in again["fixes"])


def test_presenter_confirmation_and_power(demo):
    store, agents, coordinator = demo
    early = agents.run("Move talk earlier by 5 minutes")
    fix = next(f for f in early["fixes"] if f["action"]["type"] == "pull_forward")
    with pytest.raises(ValueError, match="presenter confirmation"):
        coordinator.approve(fix["id"])
    coordinator.confirm(fix["id"], "speaker-14")
    coordinator.approve(fix["id"])

    outage = agents.run("Power is out in the east wing")
    rehome = next(f for f in outage["fixes"] if f["action"]["type"] == "rehome")
    assert len(rehome["action"]["changes"]) >= 8
    coordinator.approve(rehome["id"])
    assert store.read("MATCH (r:Room {wing:'east'}) WHERE r.has_power=false RETURN count(r)")[0][0] == 2
    assert store.read("MATCH (t:Talk)-[:IN_ROOM]->(r:Room {wing:'east'}) RETURN count(t)")[0][0] == 0


def test_panel_and_track_proposals(demo):
    store, agents, _ = demo
    # These proposals are checked without applying additional schedule changes.
    panel = agents.run("Panel first speaker ran 25 minutes over")
    assert any(f["action"]["type"] == "panel_budget" and f["status"] == "proposed" for f in panel["fixes"])
    cancelled = agents.run("Panel cancelled")
    assert any("NLP Late-Breaking" in f["proposal"] for f in cancelled["fixes"])
    assert any(f["rejected_rule"] == "related session has no open future slot" for f in cancelled["fixes"])
    # The outage has removed East-wing room options, so the wrong-track scan may
    # propose or reject a move, but it must explain the result in a Fix node.
    wrong = agents.run("Talk 18 is in the wrong track")
    assert wrong["fixes"]
