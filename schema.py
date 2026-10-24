# Blast Radius graph schema. Times = minutes since midnight (600 = 10:00).
 
# node label -> required properties
NODES = {
    "Room":         ["id", "name", "capacity", "wing", "has_power"],
    "Session":      ["id", "title", "subfield", "start", "end"],
    "Talk":         ["id", "title", "start", "end", "status"],
    "Person":       ["id", "name", "checked_in", "role"],
    "Topic":        ["id", "name"],
    "SideEvent":    ["id", "name", "start", "capacity"],
    "Catering":     ["id", "item", "servings", "time"],
    "Incident":     ["id", "type", "desc", "ts"],
    "Fix":          ["id", "proposal", "status"],
    "Notification": ["id", "body", "status"],
    "Agent":        ["id", "name"],
}
 
# edge type -> (allowed from labels, allowed to labels; None = any)
RELS = {
    "IN_ROOM":    ({"Talk"}, {"Room"}),
    "IN_SESSION": ({"Talk"}, {"Session"}),            # prop: order
    "NEXT_IN":    ({"Talk"}, {"Talk"}),               # room running order
    "PRESENTS":   ({"Person"}, {"Talk"}),
    "CHAIRS":     ({"Person"}, {"Session"}),
    "ABOUT":      ({"Talk"}, {"Topic"}),
    "EXPERT_IN":  ({"Person"}, {"Topic"}),
    "COAUTHORED": ({"Person"}, {"Person"}),           # create one way, query with -[]-
    "RSVP":       ({"Person"}, {"Talk", "SideEvent"}),
    "FOR":        ({"Catering"}, {"SideEvent", "Room"}),
    "WALK":       ({"Room"}, {"Room"}),               # P2, props: mins, stairs
    "ASKED":      ({"Person"}, {"Talk"}),             # P2, prop: duration
    "AFFECTS":    ({"Incident"}, None),
    "CAUSED":     ({"Incident"}, {"Incident"}),       # the cascade
    "RESOLVES":   ({"Fix"}, {"Incident"}),
    "PROPOSED":   ({"Agent"}, {"Fix"}),
    "DRAFTED":    ({"Agent"}, {"Notification"}),
    "HAS_NOTIFICATION": ({"Fix"}, {"Notification"}),
    "TO":         ({"Notification"}, {"Person"}),
}
 
# fixer rule names (Fix.rejected_rule must be one of these)
RULES = {
    "SAME_SUBFIELD_OVERLAP": "Two same-subfield sessions can't overlap",
    "MIN_BREAK":             "Breaks can't drop below 15 min",
    "LATE_DAY_CONCURRENCY":  "Fewer parallel sessions after 16:00",
    "JUNIOR_LAST_SLOT":      "Don't put junior presenters in the last slot",
}
 
 
# clock time -> minutes since midnight
def hhmm(h, m=0):
    return h * 60 + m
 
 
# create a node, crash if label unknown or required prop missing
def make_node(g, label, **props):
    if label not in NODES:
        raise ValueError(f"unknown label {label}")
    missing = [p for p in NODES[label] if p not in props]
    if missing:
        raise ValueError(f"{label} missing {missing}")
    g.query(f"CREATE (n:{label}) SET n = $p", {"p": props})
 
 
# create an edge between two nodes by id, crash if type/direction wrong or nodes not found
def link(g, from_label, from_id, rel, to_label, to_id, **props):
    if rel not in RELS:
        raise ValueError(f"unknown rel {rel}")
    frm, to = RELS[rel]
    if from_label not in frm or (to is not None and to_label not in to):
        raise ValueError(f"{rel} can't go {from_label} -> {to_label}")
    res = g.query(
        f"MATCH (a:{from_label} {{id:$a}}), (b:{to_label} {{id:$b}}) "
        f"CREATE (a)-[r:{rel}]->(b) SET r = $p RETURN count(r)",
        {"a": from_id, "b": to_id, "p": props},
    )
    if res.result_set[0][0] == 0:
        raise ValueError(f"node not found: {from_id} or {to_id}")
 
 
# wipe the graph; call before seeding or you get duplicates
def reset(g):
    try:
        g.delete()
    except Exception:
        pass
 
 
# list labels/edge types in the DB that aren't defined above (should be empty)
def audit(g):
    labels = {r[0] for r in g.query("CALL db.labels()").result_set}
    rels = {r[0] for r in g.query("CALL db.relationshipTypes()").result_set}
    return {"bad_labels": labels - set(NODES), "bad_rels": rels - set(RELS)}