"""Persistência do grafo: <project_dir>/.zflow/graph.json.

Mesmo padrão do Zayder (.zayder/config.json): JSON por projeto, leitura com
utf-8-sig para tolerar BOM de quem editou no Notepad/PowerShell.
"""

from __future__ import annotations

import json
from pathlib import Path

from .models import Graph

FLOW_DIR_NAME = ".zflow"
GRAPH_FILE_NAME = "graph.json"


def graph_path(project_dir: Path | str) -> Path:
    return Path(project_dir).expanduser().resolve() / FLOW_DIR_NAME / GRAPH_FILE_NAME


def load_graph(project_dir: Path | str) -> Graph:
    """Carrega o grafo do projeto; ausente vira grafo vazio."""
    path = graph_path(project_dir)
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return Graph.model_validate(data)
    return Graph()


def save_graph(graph: Graph, project_dir: Path | str) -> Path:
    path = graph_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(graph.model_dump(mode="json"), indent=2, ensure_ascii=False),
                    encoding="utf-8")
    return path
