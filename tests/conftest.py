from __future__ import annotations

import pytest

from zflow.config import Settings
from zflow.models import Edge, Graph, Node


@pytest.fixture
def project_dir(tmp_path):
    return tmp_path


@pytest.fixture
def settings(project_dir):
    return Settings(project_dir=project_dir)


def make_node(nid: str, **kw) -> Node:
    kw.setdefault("name", f"Agente {nid}")
    return Node(id=nid, **kw)


@pytest.fixture
def diamond_graph() -> Graph:
    """a → (b, c) → d — fan-out e fan-in num grafo só."""
    return Graph(
        nodes=[make_node("a"), make_node("b"), make_node("c"), make_node("d")],
        edges=[
            Edge(id="e1", source="a", target="b"),
            Edge(id="e2", source="a", target="c"),
            Edge(id="e3", source="b", target="d"),
            Edge(id="e4", source="c", target="d"),
        ],
    )
