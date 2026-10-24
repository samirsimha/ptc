import json
import unittest
from types import SimpleNamespace

from falkordb import FalkorDB

import agents
import schema
import seed


def stub_model_client(payload):
    def create(**kwargs):
        user_message = next(message["content"] for message in kwargs["messages"] if message["role"] == "user")
        request = json.loads(user_message)
        response_payload = payload
        if "candidates" in request:
            response_payload = {"candidate_id": request["candidates"][0]["id"]}
        message = SimpleNamespace(content=json.dumps(response_payload))
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    return SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=create,
            ),
        ),
    )


class ScheduleAgentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.graph = FalkorDB(host="localhost", port=6379).select_graph("ptc_test_agents")
        seed.seed(cls.graph)

    @classmethod
    def tearDownClass(cls):
        cls.graph.delete()

    def test_triage_interprets_natural_language_and_returns_structured_incident(self):
        incident = agents.parse_incident(
            self.graph,
            "The keynote went 70 minutes over",
            stub_model_client({
                "incident_type": "overrun",
                "target_talk_id": "talk-keynote",
                "mins": 70,
            }),
        )

        self.assertEqual(
            incident,
            {"type": "overrun", "target_talk_id": "talk-keynote", "mins": 70},
        )

    def test_triage_rejects_talk_ids_not_in_the_graph(self):
        with self.assertRaisesRegex(ValueError, "talk that does not exist"):
            agents.parse_incident(
                self.graph,
                "The keynote went 70 minutes over",
                stub_model_client({
                    "incident_type": "overrun",
                    "target_talk_id": "talk-made-up",
                    "mins": 70,
                }),
            )

    def test_triage_identifies_a_no_show_without_overrun_minutes(self):
        incident = agents.parse_incident(
            self.graph,
            "The speaker for Neural Signals in Practice did not show up",
            stub_model_client({
                "incident_type": "no_show",
                "target_talk_id": "talk-noshow",
            }),
        )

        self.assertEqual(
            incident,
            {"type": "no_show", "target_talk_id": "talk-noshow"},
        )

    def test_no_show_pipeline_stages_substitute_and_drafts_notifications(self):
        result = agents.run_pipeline(
            self.graph,
            "The speaker for Neural Signals in Practice did not show up",
            stub_model_client({
                "incident_type": "no_show",
                "target_talk_id": "talk-noshow",
            }),
        )

        self.assertEqual(result["incident"]["type"], "no_show")
        self.assertEqual(
            result["fix"]["presenter_change"],
            {
                "talk_id": "talk-noshow",
                "from_person_id": "person-nina",
                "from_person": "Nina Patel",
                "to_person_id": "person-omar",
                "to_person": "Omar Chen",
            },
        )
        self.assertTrue(result["fix"]["substitute_is_coauthor"])
        self.assertEqual(
            {notification["recipient"] for notification in result["notifications"]},
            {"Nina Patel", "Omar Chen"},
        )
        self.assertTrue(all(
            "proposed to change from Nina Patel to Omar Chen" in notification["body"]
            for notification in result["notifications"]
        ))
        self.assertEqual(
            self.graph.query(
                "MATCH (p:Person)-[:PRESENTS]->(:Talk {id: 'talk-noshow'}) "
                "RETURN p.id"
            ).result_set,
            [["person-nina"]],
        )

    def test_pipeline_records_incident_and_stages_fix(self):
        original_times = self.graph.query(
            "MATCH (t:Talk {id: 'talk-followup'}) RETURN t.start, t.end"
        ).result_set
        result = agents.run_pipeline(
            self.graph,
            "The keynote went 10 minutes over",
            stub_model_client({
                "incident_type": "overrun",
                "target_talk_id": "talk-keynote",
                "mins": 10,
            }),
        )

        self.assertEqual(result["incident"]["type"], "overrun")
        self.assertEqual(result["fix"]["status"], "proposed")
        self.assertIn("passed schedule checks", result["note"])
        self.assertEqual(
            {notification["recipient"] for notification in result["notifications"]},
            {"Jules Park", "Riley Chen"},
        )
        self.assertTrue(all(
            notification["status"] == "draft"
            for notification in result["notifications"]
        ))
        self.assertEqual(
            self.graph.query(
                "MATCH (t:Talk {id: 'talk-followup'}) RETURN t.start, t.end"
            ).result_set,
            original_times,
        )

    def test_comms_drafts_notifications_only_for_people_on_moved_talks(self):
        incident = agents.record_incident(
            self.graph,
            "The keynote went 10 minutes over",
            stub_model_client({
                "incident_type": "overrun",
                "target_talk_id": "talk-keynote",
                "mins": 10,
            }),
        )
        fix = agents.schedule_overrun(self.graph, incident)

        notifications = agents.draft_notifications(self.graph, fix, fix["id"])

        self.assertEqual(
            {notification["recipient"] for notification in notifications},
            {"Jules Park", "Riley Chen"},
        )
        rows = self.graph.query(
            "MATCH (agent:Agent {id: 'agent-comms'})-[:DRAFTED]->"
            "(n:Notification)-[:TO]->(person:Person), "
            "(f:Fix {id: $fix_id})-[:HAS_NOTIFICATION]->(n) "
            "RETURN person.name, n.status, n.body ORDER BY person.name",
            {"fix_id": fix["id"]},
        ).result_set
        self.assertEqual({row[0] for row in rows}, {"Jules Park", "Riley Chen"})
        self.assertTrue(all(row[1] == "draft" for row in rows))
        self.assertTrue(all("organizer approves" in row[2] for row in rows))

    def test_schedule_agent_stages_overrun_changes_without_applying_them(self):
        incident = agents.record_incident(
            self.graph,
            "The keynote went 10 minutes over",
            stub_model_client({
                "incident_type": "overrun",
                "target_talk_id": "talk-keynote",
                "mins": 10,
            }),
        )
        original_times = self.graph.query(
            "MATCH (t:Talk {id: 'talk-followup'}) RETURN t.start, t.end"
        ).result_set

        fix = agents.schedule_overrun(self.graph, incident)

        self.assertEqual(fix["status"], "proposed")
        self.assertEqual(
            fix["staged_changes"],
            [{"talk_id": "talk-followup", "start": 610, "end": 640}],
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

    def test_schedule_uses_ai_selected_alternative_to_resolve_attendee_conflict(self):
        incident = agents.record_incident(
            self.graph,
            "The keynote went 70 minutes over",
            stub_model_client({
                "incident_type": "overrun",
                "target_talk_id": "talk-keynote",
                "mins": 70,
            }),
        )
        fixes_before = self.graph.query("MATCH (f:Fix) RETURN count(f)").result_set[0][0]
        original_times = self.graph.query(
            "MATCH (t:Talk) WHERE t.id IN ['talk-followup', 'talk-workshop'] "
            "RETURN t.id, t.start, t.end ORDER BY t.id"
        ).result_set

        fix = agents.schedule_overrun(
            self.graph,
            incident,
            stub_model_client({
                "incident_type": "overrun",
                "target_talk_id": "talk-keynote",
                "mins": 70,
            }),
        )

        fixes_after = self.graph.query("MATCH (f:Fix) RETURN count(f)").result_set[0][0]
        self.assertEqual(fixes_after, fixes_before + 1)
        self.assertEqual(fix["selected_candidate_id"], "candidate-1")
        staged_by_id = {change["talk_id"]: change for change in fix["staged_changes"]}
        self.assertEqual(staged_by_id["talk-followup"]["start"], 670)
        self.assertLessEqual(
            staged_by_id["talk-followup"]["end"],
            staged_by_id["talk-workshop"]["start"],
        )
        talks, sessions, presenters, attendees = agents._schedule_snapshot(self.graph)
        conflicts = agents._schedule_conflicts(
            talks,
            sessions,
            presenters,
            attendees,
            fix["staged_changes"],
            fix["staged_session_changes"],
        )
        self.assertEqual(conflicts, [])
        current_times = self.graph.query(
            "MATCH (t:Talk) WHERE t.id IN ['talk-followup', 'talk-workshop'] "
            "RETURN t.id, t.start, t.end ORDER BY t.id"
        ).result_set
        self.assertEqual(current_times, original_times)

    def test_schedule_rejects_room_collision_before_creating_fix(self):
        graph = FalkorDB(host="localhost", port=6379).select_graph("ptc_test_room_collision")
        seed.seed(graph)
        try:
            schema.make_node(
                graph,
                "Talk",
                id="talk-room-blocker",
                title="Room blocker",
                start=610,
                end=640,
                status="scheduled",
            )
            schema.link(
                graph,
                "Talk",
                "talk-room-blocker",
                "IN_ROOM",
                "Room",
                "room-west-main",
            )
            incident = agents.record_incident(
                graph,
                "The keynote went 10 minutes over",
                stub_model_client({
                    "incident_type": "overrun",
                    "target_talk_id": "talk-keynote",
                    "mins": 10,
                }),
            )
            fixes_before = graph.query("MATCH (f:Fix) RETURN count(f)").result_set[0][0]

            with self.assertRaisesRegex(ValueError, "No safe schedule alternative"):
                agents.schedule_overrun(graph, incident)

            fixes_after = graph.query("MATCH (f:Fix) RETURN count(f)").result_set[0][0]
            self.assertEqual(fixes_after, fixes_before)
        finally:
            graph.delete()


if __name__ == "__main__":
    unittest.main()
