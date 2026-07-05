from __future__ import annotations

import pytest

from zflow.gallery import list_templates, load_template
from zflow.models import validate_runnable
from zflow.traits import TRAITS


def test_gallery_lists_all_templates():
    items = list_templates()
    ids = {t["id"] for t in items}
    assert {"painel_critico", "hierarquia", "evolutivo", "dupla_codadora"} <= ids
    assert all(t["name"] and t["description"] and t["nodes"] > 0 for t in items)


@pytest.mark.parametrize("tid", ["painel_critico", "hierarquia", "evolutivo", "dupla_codadora"])
def test_every_template_is_valid_and_runnable(tid):
    graph = load_template(tid)
    validate_runnable(graph)  # não levanta: sem ciclo, não-vazio


@pytest.mark.parametrize("tid", ["painel_critico", "hierarquia", "evolutivo", "dupla_codadora"])
def test_template_traits_exist_in_catalog(tid):
    graph = load_template(tid)
    for n in graph.nodes:
        for t in n.traits:
            assert t in TRAITS, f"trait desconhecido '{t}' no template {tid}"


def test_dupla_codadora_has_coder_flag():
    items = {t["id"]: t for t in list_templates()}
    assert items["dupla_codadora"]["has_coder"] is True
    assert items["painel_critico"]["has_coder"] is False


def test_unknown_template_raises():
    with pytest.raises(KeyError):
        load_template("nao-existe")


def test_load_endpoint(project_dir, diamond_graph):
    from fastapi.testclient import TestClient
    from zflow.config import Settings
    from zflow.server import create_app

    c = TestClient(create_app(Settings(project_dir=project_dir)))
    assert c.get("/api/templates").status_code == 200
    r = c.post("/api/templates/hierarquia/load")
    assert r.status_code == 200
    assert len(r.json()["nodes"]) == 5
    # persistiu como grafo do projeto
    assert len(c.get("/api/graph").json()["nodes"]) == 5
    assert c.post("/api/templates/nao-existe/load").status_code == 404
