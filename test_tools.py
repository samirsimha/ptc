import unittest

from falkordb import FalkorDB

import schema
import seed
import tools


class ToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.graph = FalkorDB(host="localhost", port=6379).select_graph("ptc_test_tools")
        seed.seed(cls.graph)

    @classmethod
    def tearDownClass(cls):
        cls.graph.delete()

    def test_overrun_cascade_reports_projected_times_and_attendee_conflicts(self):
        result = tools.overrun_cascade(self.graph, "talk-keynote", 70)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "talk-followup")
        self.assertEqual(result[0]["projected_start"], 670)
        self.assertEqual(result[0]["projected_end"], 700)
        self.assertEqual(
            result[0]["attendee_conflicts"],
            [{
                "person_id": "person-rsvp-test",
                "person": "Riley Chen",
                "conflicting_talk_id": "talk-workshop",
                "conflicting_talk": "Reliable Graph Systems",
            }],
        )

    def test_overrun_cascade_does_not_change_talk_times(self):
        before = self.graph.query(
            "MATCH (t:Talk {id: 'talk-followup'}) RETURN t.start, t.end"
        ).result_set

        tools.overrun_cascade(self.graph, "talk-keynote", 70)

        after = self.graph.query(
            "MATCH (t:Talk {id: 'talk-followup'}) RETURN t.start, t.end"
        ).result_set
        self.assertEqual(after, before)

    def test_find_substitutes_prioritizes_checked_in_available_coauthor(self):
        result = tools.find_substitutes(self.graph, "talk-noshow")

        self.assertEqual(
            result,
            [{"id": "person-omar", "name": "Omar Chen", "is_coauthor": True}],
        )

    def test_oversold_events_includes_capacity_rsvps_and_catering(self):
        result = tools.oversold_events(self.graph)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "event-lab-tour")
        self.assertEqual(result[0]["capacity"], 100)
        self.assertEqual(result[0]["rsvps"], 300)
        self.assertEqual(result[0]["catering_servings"], 100)

    def test_seeded_graph_matches_schema(self):
        audit = schema.audit(self.graph)

        self.assertEqual(audit, {"bad_labels": set(), "bad_rels": set()})


if __name__ == "__main__":
    unittest.main()
