from __future__ import annotations

import threading

import pytest

from zflow.executor import GraphExecutor
from zflow.models import Edge, Graph, NodeOutput
from zflow.nodes import build_user_message

from conftest import make_node


def echo_text_fn(node, user_msg, settings, history=None):
    return NodeOutput(node_id=node.id, text=f"eco de {node.name}",
                      tokens_in=100, tokens_out=50, cost_usd=0.01)


def failing_text_fn(fail_ids):
    def fn(node, user_msg, settings, history=None):
        if node.id in fail_ids:
            raise RuntimeError("modelo caiu")
        return echo_text_fn(node, user_msg, settings)
    return fn


def collect_events():
    events = []
    return events, lambda kind, **data: events.append({"kind": kind, **data})


def test_diamond_runs_everything(diamond_graph, settings):
    events, emit = collect_events()
    ex = GraphExecutor(diamond_graph, settings, emit=emit, text_fn=echo_text_fn)
    report = ex.run("tarefa X")
    assert report.completed and not report.aborted
    assert set(report.outputs) == {"a", "b", "c", "d"}
    assert report.cost_usd_total == pytest.approx(0.04)
    kinds = [e["kind"] for e in events]
    assert kinds[0] == "run_start" and kinds[-1] == "run_done"
    assert kinds.count("node_output") == 4


def test_fan_in_receives_predecessor_messages(diamond_graph, settings):
    captured = {}

    def spy_fn(node, user_msg, settings, history=None):
        captured[node.id] = user_msg
        return echo_text_fn(node, user_msg, settings)

    GraphExecutor(diamond_graph, settings, text_fn=spy_fn).run("tarefa X")
    msg_d = captured["d"]
    assert "## Tarefa" in msg_d and "tarefa X" in msg_d
    assert "### de Agente b:" in msg_d and "eco de Agente b" in msg_d
    assert "### de Agente c:" in msg_d
    # nó-fonte recebe só a tarefa
    assert "Mensagens recebidas" not in captured["a"]


def test_error_skips_only_dependents(settings):
    # a → b → d ; c → d  (b falha; d ainda roda com a mensagem de c)
    g = Graph(
        nodes=[make_node("a"), make_node("b"), make_node("c"), make_node("d")],
        edges=[
            Edge(id="e1", source="a", target="b"),
            Edge(id="e2", source="b", target="d"),
            Edge(id="e3", source="c", target="d"),
        ],
    )
    report = GraphExecutor(g, settings, text_fn=failing_text_fn({"b"})).run("t")
    assert report.outputs["b"].error
    assert not report.outputs["d"].skipped
    # e um nó cujo ÚNICO predecessor falhou é pulado
    g2 = Graph(
        nodes=[make_node("a"), make_node("b")],
        edges=[Edge(id="e1", source="a", target="b")],
    )
    report2 = GraphExecutor(g2, settings, text_fn=failing_text_fn({"a"})).run("t")
    assert report2.outputs["b"].skipped


def test_cost_ceiling_stops_between_levels(settings):
    g = Graph(
        nodes=[make_node("a"), make_node("b")],
        edges=[Edge(id="e1", source="a", target="b")],
        cost_ceiling_usd=0.005,  # menor que o custo do primeiro nível (0.01)
    )
    events, emit = collect_events()
    report = GraphExecutor(g, settings, emit=emit, text_fn=echo_text_fn).run("t")
    assert report.aborted and not report.completed
    assert "b" not in report.outputs
    assert any(e["kind"] == "ceiling" for e in events)


def test_abort_before_level(diamond_graph, settings):
    ex = GraphExecutor(diamond_graph, settings, text_fn=echo_text_fn)
    ex.request_abort()
    report = ex.run("t")
    assert report.aborted and not report.outputs


def test_parallel_within_level(settings):
    # 4 nós-fonte com barreira: só passa se rodarem juntos
    barrier = threading.Barrier(4, timeout=5)

    def barrier_fn(node, user_msg, s, history=None):
        barrier.wait()
        return echo_text_fn(node, user_msg, s)

    g = Graph(nodes=[make_node(f"n{i}") for i in range(4)])
    report = GraphExecutor(g, settings, text_fn=barrier_fn).run("t")
    assert report.completed and len(report.outputs) == 4


def test_coder_gate_skip_and_abort(settings):
    g = Graph(
        nodes=[make_node("c1", type="coder"), make_node("c2", type="coder")],
        approve_coder=True,
    )
    decisions = iter(["skip", "abort"])
    report = GraphExecutor(
        g, settings, text_fn=echo_text_fn,
        coder_fn=lambda n, m, s: NodeOutput(node_id=n.id, text="editou"),
        gate=lambda msg: next(decisions),
    ).run("t")
    assert report.outputs["c1"].skipped
    assert report.aborted


def test_coder_gate_approve_runs_coder(settings):
    g = Graph(nodes=[make_node("c1", type="coder")], approve_coder=True)
    report = GraphExecutor(
        g, settings, text_fn=echo_text_fn,
        coder_fn=lambda n, m, s: NodeOutput(node_id=n.id, text="editou", commit_sha="abc123"),
        gate=lambda msg: "approve",
    ).run("t")
    assert report.outputs["c1"].commit_sha == "abc123"
    assert report.completed


def test_build_user_message_without_task():
    n = make_node("x", include_task=False)
    msg = build_user_message("tarefa", n, [("Fulano", "olá")])
    assert "Tarefa" not in msg
    assert "### de Fulano:" in msg


def test_retry_succeeds_on_second_attempt(settings):
    calls = {"n": 0}

    def flaky(node, user_msg, s, history=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("API fora do ar")
        return echo_text_fn(node, user_msg, s)

    settings.max_attempts = 3
    g = Graph(nodes=[make_node("a")])
    events, emit = collect_events()
    report = GraphExecutor(g, settings, emit=emit, text_fn=flaky).run("t")
    assert report.outputs["a"].attempts == 2
    assert not report.outputs["a"].error
    retries = [e for e in events if e["kind"] == "node_retry"]
    assert len(retries) == 1 and retries[0]["attempt"] == 2


def test_retry_gives_up_after_max_attempts(settings):
    settings.max_attempts = 2
    g = Graph(nodes=[make_node("a")])
    report = GraphExecutor(g, settings, text_fn=failing_text_fn({"a"})).run("t")
    out = report.outputs["a"]
    assert out.error and out.attempts == 2


def test_memory_history_is_passed_and_appended(settings):
    seen = {}

    def spy(node, user_msg, s, history=None):
        seen["history"] = history
        return echo_text_fn(node, user_msg, s)

    memory = {"a": [{"user": "pergunta antiga", "assistant": "resposta antiga"}]}
    g = Graph(nodes=[make_node("a", memory=True)])
    GraphExecutor(g, settings, text_fn=spy, memory=memory).run("t")
    assert seen["history"][0]["assistant"] == "resposta antiga"
    # a nova troca foi lembrada
    assert len(memory["a"]) == 2
    assert memory["a"][-1]["assistant"] == "eco de Agente a"


def test_memory_off_passes_none_and_does_not_append(settings):
    seen = {}

    def spy(node, user_msg, s, history=None):
        seen["history"] = history
        return echo_text_fn(node, user_msg, s)

    memory = {"a": [{"user": "x", "assistant": "y"}]}
    g = Graph(nodes=[make_node("a", memory=False)])
    GraphExecutor(g, settings, text_fn=spy, memory=memory).run("t")
    assert seen["history"] is None
    assert len(memory["a"]) == 1


def test_memory_capped_at_max_exchanges(settings):
    from zflow.store import MEMORY_MAX_EXCHANGES

    memory = {}
    g = Graph(nodes=[make_node("a", memory=True)])
    for _ in range(MEMORY_MAX_EXCHANGES + 3):
        GraphExecutor(g, settings, text_fn=echo_text_fn, memory=memory).run("t")
    assert len(memory["a"]) == MEMORY_MAX_EXCHANGES


def test_persona_instance_sees_earlier_context_in_same_run(settings):
    """Gerador → Revisor → Gerador (mesma persona): a 2ª instância recebe o que
    a 1ª disse como histórico, além do feedback do Revisor via seta."""
    seen = {}

    def spy(node, user_msg, s, history=None):
        seen[node.id] = {"history": history, "msg": user_msg}
        return NodeOutput(node_id=node.id, text=f"resposta de {node.id}")

    g = Graph(
        nodes=[
            make_node("g1", name="Gerador"),
            make_node("rev", name="Revisor"),
            make_node("g2", name="Gerador", persona="g1"),
        ],
        edges=[
            Edge(id="e1", source="g1", target="rev"),
            Edge(id="e2", source="rev", target="g2"),
        ],
    )
    GraphExecutor(g, settings, text_fn=spy).run("faça um jogo")
    # a 1ª instância não tem histórico
    assert seen["g1"]["history"] is None
    # a 2ª instância lembra a resposta da 1ª (mesma persona)...
    assert seen["g2"]["history"][-1]["assistant"] == "resposta de g1"
    # ...e recebe o feedback do Revisor pela seta
    assert "resposta de rev" in seen["g2"]["msg"]
    # o Revisor (persona própria) não herda nada
    assert seen["rev"]["history"] is None


def test_persona_memory_persists_under_shared_key(settings):
    memory = {}
    g = Graph(
        nodes=[
            make_node("g1", name="Gerador", memory=True),
            make_node("g2", name="Gerador", persona="g1", memory=True),
        ],
        edges=[Edge(id="e1", source="g1", target="g2")],
    )
    GraphExecutor(g, settings, text_fn=echo_text_fn, memory=memory).run("t")
    # as duas trocas ficam na MESMA chave (a persona), não uma por nó
    assert set(memory) == {"g1"}
    assert len(memory["g1"]) == 2


def test_save_files_auto_saves_named_blocks(settings, project_dir):
    def coder_like_text(node, user_msg, s, history=None):
        return NodeOutput(node_id=node.id,
                          text="pronto:\n```python hello.py\nprint('oi')\n```",
                          cost_usd=0.01)

    g = Graph(nodes=[make_node("a", save_files=True)])
    events, emit = collect_events()
    report = GraphExecutor(g, settings, emit=emit, text_fn=coder_like_text).run("t")
    assert report.outputs["a"].files_saved == ["hello.py"]
    assert (project_dir / "hello.py").is_file()
    out_ev = next(e for e in events if e["kind"] == "node_output")
    assert out_ev["files_saved"] == ["hello.py"]
