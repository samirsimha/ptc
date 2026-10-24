def overrun_cascade(g, talk_id, mins):
    rows = g.query(
        "MATCH (:Talk {id: $talk_id})-[:NEXT_IN*1..]->(affected:Talk) "
        "RETURN affected.id, affected.title, affected.start, affected.end, "
        "affected.start + $mins, affected.end + $mins "
        "ORDER BY affected.start",
        {"talk_id": talk_id, "mins": mins},
    ).result_set
    return [
        {
            "id": row[0],
            "title": row[1],
            "start": row[2],
            "end": row[3],
            "projected_start": row[4],
            "projected_end": row[5],
        }
        for row in rows
    ]