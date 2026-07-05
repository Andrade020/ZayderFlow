from __future__ import annotations

from zflow.models import Graph
from zflow.store import graph_path, load_graph, save_graph

from conftest import make_node


def test_load_missing_returns_empty_graph(project_dir):
    g = load_graph(project_dir)
    assert g.nodes == [] and g.edges == []


def test_save_and_load_roundtrip(project_dir, diamond_graph):
    save_graph(diamond_graph, project_dir)
    loaded = load_graph(project_dir)
    assert loaded == diamond_graph
    assert graph_path(project_dir).is_file()


def test_save_preserves_positions_and_traits(project_dir):
    g = Graph(nodes=[make_node("a", x=123.5, y=-7, traits=["rigoroso", "conciso"])])
    save_graph(g, project_dir)
    n = load_graph(project_dir).nodes[0]
    assert n.x == 123.5 and n.y == -7
    assert n.traits == ["rigoroso", "conciso"]


def test_load_tolerates_bom(project_dir, diamond_graph):
    path = save_graph(diamond_graph, project_dir)
    raw = path.read_text(encoding="utf-8")
    path.write_text(raw, encoding="utf-8-sig")  # simula edição no Notepad
    assert load_graph(project_dir) == diamond_graph
