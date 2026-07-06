from __future__ import annotations

from zflow.presets import CODER_PRESETS, TEXT_PRESETS, all_presets
from zflow.traits import TRAITS


def test_presets_have_required_fields():
    for p in TEXT_PRESETS + CODER_PRESETS:
        assert p["id"] and p["label"] and p["desc"] and p["name"]


def test_preset_traits_exist_in_catalog():
    for p in TEXT_PRESETS + CODER_PRESETS:
        for t in p.get("traits", []):
            assert t in TRAITS, f"trait desconhecido '{t}' no preset {p['id']}"


def test_blank_preset_first():
    assert TEXT_PRESETS[0]["id"] == "em_branco"
    assert CODER_PRESETS[0]["id"] == "codador"


def test_gerador_codigo_saves_files():
    p = next(p for p in TEXT_PRESETS if p["id"] == "gerador_codigo")
    assert p["save_files"] is True


def test_presets_endpoint(project_dir):
    from fastapi.testclient import TestClient
    from zflow.config import Settings
    from zflow.server import create_app

    c = TestClient(create_app(Settings(project_dir=project_dir)))
    d = c.get("/api/presets").json()
    assert d == all_presets()
    assert {p["id"] for p in d["text"]} >= {"em_branco", "critico", "gerador_codigo"}
