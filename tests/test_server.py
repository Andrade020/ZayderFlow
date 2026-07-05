from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from zflow.config import Settings
from zflow.server import create_app
from zflow.store import load_graph


@pytest.fixture
def client(project_dir):
    app = create_app(Settings(project_dir=project_dir))
    return TestClient(app)


def test_index_serves_html(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "ZayderFlow" in r.text


def test_state_starts_idle(client):
    st = client.get("/api/state").json()
    assert st["phase"] == "idle"
    assert st["seq"] == 0
    assert st["graph"]["nodes"] == []


def test_put_and_get_graph(client, project_dir, diamond_graph):
    r = client.put("/api/graph", json={"graph": diamond_graph.model_dump(mode="json")})
    assert r.status_code == 200
    got = client.get("/api/graph").json()
    assert [n["id"] for n in got["nodes"]] == ["a", "b", "c", "d"]
    # persistiu no disco
    assert len(load_graph(project_dir).nodes) == 4


def test_put_graph_invalid_is_422(client):
    bad = {"nodes": [{"id": "a", "name": "A"}, {"id": "a", "name": "A2"}], "edges": []}
    r = client.put("/api/graph", json={"graph": bad})
    assert r.status_code == 422


def test_put_graph_blocked_while_running_is_409(client, diamond_graph):
    client.app.state.manager.phase = "running"
    r = client.put("/api/graph", json={"graph": diamond_graph.model_dump(mode="json")})
    assert r.status_code == 409


def test_traits_endpoint(client):
    items = client.get("/api/traits").json()
    keys = {i["key"] for i in items}
    assert {"rigoroso", "critico", "elogiador"} <= keys
    assert all(i["label"] and i["prompt"] for i in items)


def test_models_endpoint(client):
    models = client.get("/api/models").json()["models"]
    assert "deepseek-v4-flash" in models


def test_logo_and_favicon(client):
    assert client.get("/logo.png").status_code == 200
    assert client.get("/favicon.ico").status_code == 200
