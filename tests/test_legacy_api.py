"""The starter repository's public entry point still serves the graph."""

from fastapi.testclient import TestClient

from main import app


def test_root_entry_point_and_graph():
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        result = client.get("/graph")
        assert result.status_code == 200
        body = result.json()
        assert any(node["label"] == "Talk" for node in body["nodes"])
        assert any(edge["type"] == "IN_ROOM" for edge in body["links"])
