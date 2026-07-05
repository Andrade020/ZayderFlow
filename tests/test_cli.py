from __future__ import annotations

import argparse
import json
import socket

import zflow.cli as cli


def test_pick_port_returns_preferred_when_free():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    free = s.getsockname()[1]
    s.close()
    assert cli._pick_port(free) == free


def test_pick_port_falls_back_when_busy():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    busy = s.getsockname()[1]
    try:
        assert cli._pick_port(busy) != busy
    finally:
        s.close()


def test_bare_command_launches_ui(monkeypatch):
    called = {}
    monkeypatch.setattr(cli, "cmd_ui", lambda args: called.update(vars(args)) or 0)
    assert cli.main([]) == 0
    assert called == {"dir": ".", "port": 8430, "no_browser": False}


def _fake_completion(monkeypatch):
    import litellm

    class Usage(dict):
        pass

    def fake(model, messages, timeout=None, **kw):
        class Msg:
            content = f"resposta fake de {model}"

        class Choice:
            message = Msg()

        class Resp:
            choices = [Choice()]
            usage = {"prompt_tokens": 10, "completion_tokens": 5}

        return Resp()

    monkeypatch.setattr(litellm, "completion", fake)


def test_run_headless(tmp_path, monkeypatch, capsys):
    _fake_completion(monkeypatch)
    graph = {
        "name": "mini",
        "nodes": [
            {"id": "a", "name": "Analista", "type": "text"},
            {"id": "b", "name": "Sintetizador", "type": "text"},
        ],
        "edges": [{"id": "e1", "source": "a", "target": "b"}],
    }
    gpath = tmp_path / "g.json"
    gpath.write_text(json.dumps(graph), encoding="utf-8")
    rc = cli.cmd_run(argparse.Namespace(
        task="teste", graph=str(gpath), dir=str(tmp_path), yes=True))
    out = capsys.readouterr().out
    assert rc == 0
    assert "Analista" in out and "Sintetizador" in out
    assert "custo total" in out
    assert "resposta fake" in out


def test_run_headless_empty_graph_errors(tmp_path, capsys):
    gpath = tmp_path / "g.json"
    gpath.write_text('{"name": "vazio", "nodes": [], "edges": []}', encoding="utf-8")
    rc = cli.cmd_run(argparse.Namespace(task="t", graph=str(gpath), dir=str(tmp_path), yes=True))
    assert rc == 2
    assert "vazio" in capsys.readouterr().out
