def overrun_cascade(g, talk_id, mins):
    # finds all future talks in the same room affected by a meeting running over time and calculates their start and end time
    rows = g.query(
        "MATCH (:Talk {id: $talk_id})-[:NEXT_IN*1..]->(affected:Talk) "
        "RETURN affected.id, affected.title, affected.start, affected.end, "
        "affected.start + $mins, affected.end + $mins "
        "ORDER BY affected.start",
        {"talk_id": talk_id, "mins": mins},
    ).result_set
    # finds attendees who have rsvp conflicts based on affected meetings
    conflict_rows = g.query(
        "MATCH (origin:Talk {id: $talk_id})-[:NEXT_IN*1..]->(affected:Talk) "
        "MATCH (person:Person)-[:RSVP]->(affected), (person)-[:RSVP]->(other:Talk) "
        "WHERE affected.id <> other.id "
        "AND affected.start + $mins < other.end "
        "AND affected.end + $mins > other.start "
        "AND NOT (origin)-[:NEXT_IN*1..]->(other) "
        "RETURN affected.id, person.id, person.name, other.id, other.title "
        "ORDER BY affected.start, person.name",
        {"talk_id": talk_id, "mins": mins},
    ).result_set
    # for each attendee affected by meetings running over time, create a dictionary mapping person and the conflicts they rsvp'd to
    conflicts_by_talk = {}
    for conflict in conflict_rows:
        conflicts_by_talk.setdefault(conflict[0], []).append({
            "person_id": conflict[1],
            "person": conflict[2],
            "conflicting_talk_id": conflict[3],
            "conflicting_talk": conflict[4],
        })
    # for each talk running over time, it creates a list of every attendee who is affected
    affected_talks = []
    for row in rows:
        affected_talks.append({
            "id": row[0],
            "title": row[1],
            "start": row[2],
            "end": row[3],
            "projected_start": row[4],
            "projected_end": row[5],
            "attendee_conflicts": conflicts_by_talk.get(row[0], []),
        })
    return affected_talks

def find_substitutes(g, talk_id):
        # finds experts who are available/checked-in who can replace no-shows on the same topic
        rows = g.query(
            "MATCH (talk:Talk {id: $talk_id})-[:ABOUT]->(topic:Topic)<-[:EXPERT_IN]-(candidate:Person) "
            "MATCH (speaker:Person)-[:PRESENTS]->(talk) "
            "WHERE candidate.checked_in = true AND candidate.id <> speaker.id "
            "OPTIONAL MATCH (candidate)-[:PRESENTS]->(busy:Talk) "
            "WHERE busy.start < talk.end AND busy.end > talk.start "
            "WITH talk, candidate, speaker, count(busy) AS busy_talks "
            "WHERE busy_talks = 0 "
            "OPTIONAL MATCH (speaker)-[coauthored:COAUTHORED]-(candidate) "
            "RETURN candidate.id, candidate.name, count(coauthored) > 0 AS is_coauthor "
            "ORDER BY is_coauthor DESC, candidate.name",
            {"talk_id": talk_id},
        ).result_set
