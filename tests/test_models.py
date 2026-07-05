from __future__ import annotations

import pytest
from pydantic import ValidationError

from zflow.models import Edge, Graph, Node, topo_levels, validate_runnable

from conftest import make_node


def test_graph_rejects_duplicate_node_ids():
    with pytest.raises(ValidationError, match="repetidos"):
        Graph(nodes=[make_node("a"), make_node("a")])


def test_graph_rejects_edge_to_unknown_node():
    with pytest.raises(ValidationError, match="inexistente"):
        Graph(nodes=[make_node("a")], edges=[Edge(id="e1", source="a", target="zz")])


def test_edge_rejects_self_loop():
    with pytest.raises(ValidationError, match="ele mesmo"):
        Edge(id="e1", source="a", target="a")


def test_graph_rejects_duplicate_edges():
    with pytest.raises(ValidationError, match="duplicada"):
        Graph(
            nodes=[make_node("a"), make_node("b")],
            edges=[
                Edge(id="e1", source="a", target="b"),
                Edge(id="e2", source="a", target="b"),
            ],
        )


def test_graph_allows_incomplete_work_in_progress():
    # nó solto, sem arestas: precisa poder salvar enquanto desenha
    g = Graph(nodes=[make_node("solto")])
    assert g.nodes[0].id == "solto"


def test_topo_levels_diamond(diamond_graph):
    levels = [[n.id for n in level] for level in topo_levels(diamond_graph)]
    assert levels == [["a"], ["b", "c"], ["d"]]


def test_topo_levels_detects_cycle():
    g = Graph(
        nodes=[make_node("a"), make_node("b")],
        edges=[
            Edge(id="e1", source="a", target="b"),
            Edge(id="e2", source="b", target="a"),
        ],
    )
    with pytest.raises(ValueError, match="ciclo"):
        topo_levels(g)


def test_validate_runnable_rejects_empty_graph():
    with pytest.raises(ValueError, match="vazio"):
        validate_runnable(Graph())


def test_validate_runnable_accepts_diamond(diamond_graph):
    validate_runnable(diamond_graph)  # não levanta


def test_predecessors_follow_edge_order(diamond_graph):
    preds = diamond_graph.predecessors("d")
    assert [p.id for p in preds] == ["b", "c"]


def test_node_defaults():
    n = Node(id="x", name="X")
    assert n.type == "text"
    assert n.include_task is True
    assert n.traits == []
