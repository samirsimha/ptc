import json
import unittest

from falkordb import FalkorDB

import agents
import seed


class ScheduleAgentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.graph = FalkorDB(host="localhost", port=6379).select_graph("ptc_test_agents")
        seed.seed(cls.graph)

    @classmethod
    def tearDownClass(cls):
        cls.graph.delete()

    def test_schedule_agent_stages_overrun_changes_without_applying_them(self):
        incident = agents.record_incident(self.graph, "keynote running 70 over")
        original_times = self.graph.query(
            "MATCH (t:Talk {id: 'talk-followup'}) RETURN t.start, t.end"
        ).result_set

        fix = agents.schedule_overrun(self.graph, incident)

        self.assertEqual(fix["status"], "proposed")
        self.assertEqual(
            fix["staged_changes"],
            [{"talk_id": "talk-followup", "start": 670, "end": 700}],
        )
        self.assertEqual(
            fix["attendee_conflicts"]["talk-followup"][0]["person"],
            "Riley Chen",
        )
        stored_fix = self.graph.query(
            "MATCH (f:Fix {id: $id})-[:RESOLVES]->(i:Incident) "
            "RETURN f.status, f.staged_changes, i.id",
            {"id": fix["id"]},
        ).result_set[0]
        self.assertEqual(stored_fix[0], "proposed")
        self.assertEqual(json.loads(stored_fix[1]), fix["staged_changes"])
        self.assertEqual(stored_fix[2], incident["incident_id"])

        current_times = self.graph.query(
            "MATCH (t:Talk {id: 'talk-followup'}) RETURN t.start, t.end"
        ).result_set
        self.assertEqual(current_times, original_times)


if __name__ == "__main__":
    unittest.main()
