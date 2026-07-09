from __future__ import annotations

import pytest

from zflow.looping import base_id, expand_loops
from zflow.models import Edge, Graph, topo_levels, validate_runnable

from conftest import make_node


def loop_graph(rounds: int = 3) -> Graph:
    """Gerador → Crítico, com seta 🔁 de volta; Gerador → Final (fora do loop)."""
    return Graph(
        nodes=[make_node("g", name="Gerador"), make_node("c", name="Crítico"),
               make_node("f", name="Final")],
        edges=[
            Edge(id="e1", source="g", target="c"),
            Edge(id="e2", source="c", target="g", kind="loop", rounds=rounds),
            Edge(id="e3", source="g", target="f"),
        ],
    )


def test_no_loops_returns_same_graph(diamond_graph):
    assert expand_loops(diamond_graph) is diamond_graph


def test_unroll_counts_and_ids():
    g2 = expand_loops(loop_graph(rounds=3))
    ids = {n.id for n in g2.nodes}
    # corpo {g, c} clonado 2 vezes extras + Final
    assert ids == {"g", "c", "f", "g~2", "c~2", "g~3", "c~3"}
    validate_runnable(g2)  # o resultado é um DAG de verdade


def test_clones_share_persona_of_original():
    g2 = expand_loops(loop_graph())
    clone = g2.node("g~2")
    assert clone.persona == "g"  # rodada 2 lembra a rodada 1
    assert "rodada 2" in clone.name


def test_chain_connects_rounds_in_order():
    g2 = expand_loops(loop_graph(rounds=3))
    pairs = {(e.source, e.target) for e in g2.edges}
    # fim da rodada 1 (Crítico) alimenta o começo da rodada 2 (Gerador~2)...
    assert ("c", "g~2") in pairs
    assert ("c~2", "g~3") in pairs
    # ...e as arestas internas existem por rodada
    assert ("g~2", "c~2") in pairs and ("g~3", "c~3") in pairs


def test_outgoing_edge_leaves_from_last_round():
    g2 = expand_loops(loop_graph(rounds=3))
    to_final = [e for e in g2.edges if e.target == "f"]
    assert len(to_final) == 1
    assert to_final[0].source == "g~3"  # o Final vê a ÚLTIMA versão


def test_final_runs_after_all_rounds():
    g2 = expand_loops(loop_graph(rounds=2))
    levels = topo_levels(g2)
    order = {n.id: i for i, level in enumerate(levels) for n in level}
    assert order["f"] > order["g~2"] > order["c"] > order["g"]


def test_self_loop_repeats_single_node():
    g = Graph(
        nodes=[make_node("a"), make_node("b")],
        edges=[
            Edge(id="e1", source="a", target="a", kind="loop", rounds=3),
            Edge(id="e2", source="a", target="b"),
        ],
    )
    g2 = expand_loops(g)
    pairs = {(e.source, e.target) for e in g2.edges}
    assert ("a", "a~2") in pairs and ("a~2", "a~3") in pairs
    assert ("a~3", "b") in pairs  # saída rewire para a última rodada
    validate_runnable(g2)


def test_forward_loop_edge_is_friendly_error():
    g = Graph(
        nodes=[make_node("a"), make_node("b")],
        edges=[Edge(id="e1", source="a", target="b", kind="loop", rounds=2)],
    )
    with pytest.raises(ValueError, match="ANTERIOR"):
        expand_loops(g)


def test_overlapping_loops_rejected():
    g = Graph(
        nodes=[make_node("a"), make_node("b")],
        edges=[
            Edge(id="e1", source="a", target="b"),
            Edge(id="e2", source="b", target="a", kind="loop", rounds=2),
            Edge(id="e3", source="a", target="a", kind="loop", rounds=2),
        ],
    )
    with pytest.raises(ValueError, match="compartilhar"):
        expand_loops(g)


def test_rounds_are_capped():
    g2 = expand_loops(loop_graph(rounds=999))
    from zflow.looping import MAX_ROUNDS
    assert not any(n.id.endswith(f"~{MAX_ROUNDS + 1}") for n in g2.nodes)
    assert any(n.id == f"g~{MAX_ROUNDS}" for n in g2.nodes)


def test_base_id():
    assert base_id("n3~2") == "n3"
    assert base_id("n3") == "n3"


def test_normal_self_loop_still_rejected():
    with pytest.raises(ValueError):
        Edge(id="e1", source="a", target="a")  # kind normal
