import json
import unittest
from types import SimpleNamespace

from fastapi.testclient import TestClient
from falkordb import FalkorDB

import agents
import main
import schema
import seed


def triage_client():
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=json.dumps({
                    "incident_type": "overrun",
                    "target_talk_id": "talk-keynote",
                    "mins": 10,
                })),
            ),
        ],
    )
    return SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=lambda **kwargs: response,
            ),
        ),
    )


class FixDecisionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.graph = FalkorDB(host="localhost", port=6379).select_graph("ptc_test_decisions")
        cls.original_graph = main.g
        main.g = cls.graph
        cls.client = TestClient(main.app)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        main.g = cls.original_graph
        cls.graph.delete()

    def setUp(self):
        seed.seed(self.graph)

    def create_fix(self):
        return agents.run_pipeline(
            self.graph,
            "The keynote went 10 minutes over",
            triage_client(),
        )["fix"]

    def test_approve_applies_staged_talk_and_session_times(self):
        fix = self.create_fix()

        response = self.client.post(f"/fixes/{fix['id']}/approve")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"id": fix["id"], "status": "applied"})
        self.assertEqual(
            self.graph.query(
                "MATCH (t:Talk {id: 'talk-followup'}) RETURN t.start, t.end"
            ).result_set,
            [[610, 640]],
        )
        self.assertEqual(
            self.graph.query(
                "MATCH (s:Session {id: 'session-opening'}) RETURN s.start, s.end"
            ).result_set,
            [[540, 640]],
        )
        self.assertEqual(
            self.graph.query(
                "MATCH (:Fix {id: $id})-[:HAS_NOTIFICATION]->(n:Notification) "
                "RETURN collect(n.status)",
                {"id": fix["id"]},
            ).result_set[0][0],
            ["approved", "approved"],
        )

    def test_reject_marks_fix_without_changing_schedule(self):
        fix = self.create_fix()

        response = self.client.post(f"/fixes/{fix['id']}/reject")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"id": fix["id"], "status": "rejected"})
        self.assertEqual(
            self.graph.query(
                "MATCH (t:Talk {id: 'talk-followup'}) RETURN t.start, t.end"
            ).result_set,
            [[600, 630]],
        )
        self.assertEqual(
            self.graph.query(
                "MATCH (:Fix {id: $id})-[:HAS_NOTIFICATION]->(n:Notification) "
                "RETURN collect(n.status)",
                {"id": fix["id"]},
            ).result_set[0][0],
            ["cancelled", "cancelled"],
        )

    def test_explain_returns_agent_fix_incident_and_talk_path(self):
        fix = self.create_fix()

        response = self.client.get(f"/fixes/{fix['id']}/explain")

        self.assertEqual(response.status_code, 200, response.text)
        explanation = response.json()
        self.assertEqual(explanation["agent"], "Schedule")
        self.assertEqual(explanation["fix"]["id"], fix["id"])
        self.assertEqual(explanation["incident"]["type"], "overrun")
        self.assertEqual(explanation["affected_talk"]["id"], "talk-keynote")
        self.assertEqual(explanation["proposed_talks"], [
            {"id": "talk-followup", "title": "AI in the Real World"},
        ])

    def test_approve_rejects_a_fix_that_became_unsafe(self):
        fix = self.create_fix()
        schema.make_node(
            self.graph,
            "Talk",
            id="talk-new-blocker",
            title="Newly scheduled room blocker",
            start=610,
            end=640,
            status="scheduled",
        )
        schema.link(
            self.graph,
            "Talk",
            "talk-new-blocker",
            "IN_ROOM",
            "Room",
            "room-west-main",
        )

        response = self.client.post(f"/fixes/{fix['id']}/approve")

        self.assertEqual(response.status_code, 409)
        self.assertIn("no longer safe", response.json()["detail"])
        self.assertEqual(
            self.graph.query(
                "MATCH (t:Talk {id: 'talk-followup'}) RETURN t.start, t.end"
            ).result_set,
            [[600, 630]],
        )

    def test_fix_cannot_be_decided_twice(self):
        fix = self.create_fix()
        self.assertEqual(self.client.post(f"/fixes/{fix['id']}/reject").status_code, 200)

        response = self.client.post(f"/fixes/{fix['id']}/approve")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            self.graph.query(
                "MATCH (t:Talk {id: 'talk-followup'}) RETURN t.start, t.end"
            ).result_set,
            [[600, 630]],
        )


if __name__ == "__main__":
    unittest.main()
