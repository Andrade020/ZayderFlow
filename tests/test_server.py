from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from zflow.config import Settings
from zflow.models import NodeOutput
from zflow.server import create_app
from zflow.store import load_graph


def fake_text_fn(node, user_msg, settings):
    return NodeOutput(node_id=node.id, text=f"resposta de {node.name} " + "x" * 3000,
                      tokens_in=100, tokens_out=50, cost_usd=0.01)


@pytest.fixture
def client(project_dir):
    app = create_app(Settings(project_dir=project_dir), text_fn=fake_text_fn)
    return TestClient(app)


def wait_phase(client, *phases, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = client.get("/api/state").json()
        if st["phase"] in phases:
            return st
        time.sleep(0.02)
    raise AssertionError(f"não chegou em {phases}; última fase: {st['phase']}")


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


def _put_graph(client, graph):
    r = client.put("/api/graph", json={"graph": graph.model_dump(mode="json")})
    assert r.status_code == 200


def test_run_flow_end_to_end(client, diamond_graph):
    _put_graph(client, diamond_graph)
    r = client.post("/api/run", json={"task": "avalie a ideia"})
    assert r.status_code == 200
    st = wait_phase(client, "done", "error")
    assert st["phase"] == "done"
    assert set(st["node_status"].values()) == {"done"}
    assert st["actual_usd"] > 0
    kinds = [e["kind"] for e in client.get("/api/state?since=0").json()["events"]]
    assert kinds.count("node_output") == 4
    assert "run_done" in kinds


def test_run_events_carry_preview_not_full_text(client, diamond_graph):
    _put_graph(client, diamond_graph)
    client.post("/api/run", json={"task": "t"})
    wait_phase(client, "done")
    evs = [e for e in client.get("/api/state?since=0").json()["events"] if e["kind"] == "node_output"]
    assert all(len(e["preview"]) <= 2000 for e in evs)
    assert all(e["truncated"] for e in evs)  # fake gera > 2000 chars


def test_output_endpoint_returns_full_text(client, diamond_graph):
    _put_graph(client, diamond_graph)
    client.post("/api/run", json={"task": "t"})
    wait_phase(client, "done")
    d = client.get("/api/output/a").json()
    assert len(d["text"]) > 2000
    assert d["tokens_out"] == 50


def test_output_missing_is_404(client):
    assert client.get("/api/output/nope").status_code == 404


def test_run_empty_graph_is_422(client):
    r = client.post("/api/run", json={"task": "t"})
    assert r.status_code == 422


def test_run_empty_task_is_422(client, diamond_graph):
    _put_graph(client, diamond_graph)
    assert client.post("/api/run", json={"task": "  "}).status_code == 422


def test_run_while_running_is_409(client, diamond_graph, project_dir):
    slow = lambda n, m, s: (time.sleep(0.3), fake_text_fn(n, m, s))[1]
    app = create_app(Settings(project_dir=project_dir), text_fn=slow)
    c = TestClient(app)
    _put_graph(c, diamond_graph)
    c.post("/api/run", json={"task": "t"})
    assert c.post("/api/run", json={"task": "t2"}).status_code == 409
    wait_phase(c, "done")


def test_coder_gate_waits_for_approval(client, project_dir, diamond_graph):
    from zflow.models import Graph
    from conftest import make_node

    g = Graph(nodes=[make_node("c1", type="coder")], approve_coder=True)
    app = create_app(
        Settings(project_dir=project_dir),
        text_fn=fake_text_fn,
        coder_fn=lambda n, m, s: NodeOutput(node_id=n.id, text="editou", commit_sha="abc"),
    )
    c = TestClient(app)
    _put_graph(c, g)
    c.post("/api/run", json={"task": "t"})
    st = wait_phase(c, "waiting_approval")
    assert "c1" in st["pending_approval"] or "Agente c1" in st["pending_approval"]
    assert c.post("/api/approve", json={"decision": "approve"}).status_code == 200
    st = wait_phase(c, "done")
    assert st["node_status"]["c1"] == "done"


def test_approve_without_pending_is_409(client):
    assert client.post("/api/approve", json={"decision": "approve"}).status_code == 409


def test_reset_after_done(client, diamond_graph):
    _put_graph(client, diamond_graph)
    client.post("/api/run", json={"task": "t"})
    wait_phase(client, "done")
    assert client.post("/api/reset").json()["ok"]
    st = client.get("/api/state").json()
    assert st["phase"] == "idle" and st["seq"] == 0 and st["node_status"] == {}
    # o grafo continua lá
    assert len(st["graph"]["nodes"]) == 4
