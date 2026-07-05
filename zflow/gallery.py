"""Galeria de templates: arquiteturas prontas embutidas no pacote.

Cada template é um JSON com os campos de Graph + template_id + description.
As posições x/y já vêm arrumadas em colunas por nível topológico.
"""

from __future__ import annotations

import json
from pathlib import Path

from .models import Graph

TEMPLATES_DIR = Path(__file__).parent / "templates"


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def list_templates() -> list[dict]:
    items = []
    for path in sorted(TEMPLATES_DIR.glob("*.json")):
        data = _read(path)
        items.append({
            "id": data.get("template_id", path.stem),
            "name": data.get("name", path.stem),
            "description": data.get("description", ""),
            "nodes": len(data.get("nodes", [])),
            "has_coder": any(n.get("type") == "coder" for n in data.get("nodes", [])),
        })
    return items


def load_template(template_id: str) -> Graph:
    for path in TEMPLATES_DIR.glob("*.json"):
        data = _read(path)
        if data.get("template_id", path.stem) == template_id:
            data.pop("template_id", None)
            data.pop("description", None)
            return Graph.model_validate(data)
    raise KeyError(template_id)
