from __future__ import annotations

import pytest

from zflow.gallery import list_templates, load_template
from zflow.models import validate_runnable
from zflow.traits import TRAITS


ALL_TEMPLATE_IDS = [
    "painel_critico", "hierarquia", "evolutivo", "dupla_codadora",
    "revisao_codigo", "fabrica_scripts", "esquadrao_debug", "gerar_revisar_refazer",
    "refinamento_em_loop",
]


def test_gerar_revisar_refazer_uses_shared_persona():
    graph = load_template("gerar_revisar_refazer")
    instance = graph.node("n3")
    assert instance.persona == "n1"
    assert instance.name == graph.node("n1").name


def test_gallery_lists_all_templates():
    items = list_templates()
    ids = {t["id"] for t in items}
    assert set(ALL_TEMPLATE_IDS) <= ids
    assert all(t["name"] and t["description"] and t["nodes"] > 0 for t in items)


@pytest.mark.parametrize("tid", ALL_TEMPLATE_IDS)
def test_every_template_is_valid_and_runnable(tid):
    from zflow.looping import expand_loops

    graph = load_template(tid)
    validate_runnable(expand_loops(graph))  # não levanta: expande e é um DAG


def test_refinamento_em_loop_has_loop_edge():
    graph = load_template("refinamento_em_loop")
    loop = next(e for e in graph.edges if e.kind == "loop")
    assert loop.rounds == 3


def test_fabrica_scripts_generator_saves_files():
    graph = load_template("fabrica_scripts")
    gen = next(n for n in graph.nodes if "Gerador" in n.name)
    assert gen.save_files is True


@pytest.mark.parametrize("tid", ALL_TEMPLATE_IDS)
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
