from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from zflow.config import Settings
from zflow.models import NodeOutput
from zflow.server import create_app
from zflow.store import load_graph


def fake_text_fn(node, user_msg, settings, history=None):
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
    slow = lambda n, m, s, history=None: (time.sleep(0.3), fake_text_fn(n, m, s))[1]
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


def test_memory_persists_and_clears(project_dir, diamond_graph):
    from zflow.models import Graph
    from zflow.store import load_memory
    from conftest import make_node

    g = Graph(nodes=[make_node("a", memory=True)])
    app = create_app(Settings(project_dir=project_dir), text_fn=fake_text_fn)
    c = TestClient(app)
    _put_graph(c, g)
    c.post("/api/run", json={"task": "t"})
    wait_phase(c, "done")
    st = c.get("/api/state").json()
    assert st["memory_counts"] == {"a": 1}
    assert load_memory(project_dir) != {}
    # limpar só o agente
    r = c.post("/api/memory/clear", json={"node_id": "a"})
    assert r.json()["memory_counts"] == {}
    assert load_memory(project_dir) == {}


def test_files_endpoints_roundtrip(client):
    r = client.post("/api/save-file", json={"path": "sub/hello.py", "content": "print('oi')\n"})
    assert r.status_code == 200 and r.json()["path"] == "sub/hello.py"
    files = client.get("/api/files").json()["files"]
    assert any(f["path"] == "sub/hello.py" for f in files)
    d = client.get("/api/file", params={"path": "sub/hello.py"}).json()
    assert d["content"] == "print('oi')\n"


def test_save_file_outside_project_is_400(client):
    assert client.post("/api/save-file", json={"path": "../fora.py", "content": "x"}).status_code == 400
    assert client.get("/api/file", params={"path": "../fora.py"}).status_code == 400


def test_run_file_captures_stdout_and_stderr(client):
    client.post("/api/save-file", json={"path": "ok.py", "content": "print('funcionou')\n"})
    r = client.post("/api/run-file", json={"path": "ok.py"}).json()
    assert r["returncode"] == 0 and "funcionou" in r["stdout"]
    client.post("/api/save-file", json={"path": "quebra.py", "content": "raise ValueError('x')\n"})
    r = client.post("/api/run-file", json={"path": "quebra.py"}).json()
    assert r["returncode"] != 0 and "ValueError" in r["stderr"]
    assert client.post("/api/run-file", json={"path": "nao_existe.py"}).status_code == 404
    client.post("/api/save-file", json={"path": "dados.txt", "content": "x"})
    assert client.post("/api/run-file", json={"path": "dados.txt"}).status_code == 422


def test_reveal_calls_opener(client, monkeypatch):
    import zflow.server as srv

    opened = {}
    monkeypatch.setattr(srv, "_open_path", lambda p: opened.update(path=str(p)))
    assert client.post("/api/reveal").json()["ok"]
    assert opened["path"]


def test_output_blocks_endpoint(project_dir, diamond_graph):
    from zflow.models import Graph
    from conftest import make_node

    def code_text_fn(node, user_msg, settings, history=None):
        return NodeOutput(node_id=node.id,
                          text="```python hello.py\nprint('oi')\n```\n```js\nlet a=1\n```")

    g = Graph(nodes=[make_node("a", name="Gerador")])
    app = create_app(Settings(project_dir=project_dir), text_fn=code_text_fn)
    c = TestClient(app)
    _put_graph(c, g)
    c.post("/api/run", json={"task": "t"})
    wait_phase(c, "done")
    blocks = c.get("/api/output/a/blocks").json()
    assert len(blocks) == 2
    assert blocks[0]["suggested"] == "hello.py"
    assert blocks[1]["suggested"].endswith(".js")
    assert c.get("/api/output/zz/blocks").status_code == 404


def test_reset_after_done(client, diamond_graph):
    _put_graph(client, diamond_graph)
    client.post("/api/run", json={"task": "t"})
    wait_phase(client, "done")
    seq_before = client.get("/api/state").json()["seq"]
    assert client.post("/api/reset").json()["ok"]
    st = client.get("/api/state").json()
    assert st["phase"] == "idle" and st["node_status"] == {} and st["events"] == []
    # o seq NUNCA volta pra trás: o cliente guarda lastSeq e filtraria os
    # eventos da próxima execução para sempre (regressão: feed "travado")
    assert st["seq"] >= seq_before
    # o grafo continua lá
    assert len(st["graph"]["nodes"]) == 4


def test_new_run_clears_previous_events(client, diamond_graph):
    """Regressão: reload/cliente novo (since=0) não pode re-exibir o histórico
    da execução anterior — só os eventos da execução atual."""
    _put_graph(client, diamond_graph)
    client.post("/api/run", json={"task": "t1"})
    wait_phase(client, "done")
    client.post("/api/run", json={"task": "t2"})
    wait_phase(client, "done")
    kinds = [e["kind"] for e in client.get("/api/state?since=0").json()["events"]]
    assert kinds.count("run_start") == 1
    assert kinds.count("node_output") == 4


def test_events_after_reset_are_visible_to_stale_client(client, diamond_graph):
    """Regressão: cliente que viu seq=N antes do reset precisa receber os
    eventos da execução seguinte pedindo since=N."""
    _put_graph(client, diamond_graph)
    client.post("/api/run", json={"task": "t"})
    wait_phase(client, "done")
    last_seq = client.get("/api/state").json()["seq"]
    client.post("/api/reset")
    client.post("/api/run", json={"task": "t2"})
    wait_phase(client, "done")
    events = client.get(f"/api/state?since={last_seq}").json()["events"]
    kinds = [e["kind"] for e in events]
    assert "run_start" in kinds and "run_done" in kinds
    assert kinds.count("node_output") == 4
