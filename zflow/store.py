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
MEMORY_FILE_NAME = "memory.json"
MEMORY_MAX_EXCHANGES = 8  # trocas lembradas por agente (senão o contexto/custo só cresce)


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


# -- memória por agente ------------------------------------------------------
# {node_id: [{"user": ..., "assistant": ...}, ...]} — as trocas anteriores de
# cada agente, injetadas como histórico na próxima execução (se node.memory).

def memory_path(project_dir: Path | str) -> Path:
    return Path(project_dir).expanduser().resolve() / FLOW_DIR_NAME / MEMORY_FILE_NAME


def load_memory(project_dir: Path | str) -> dict[str, list[dict]]:
    path = memory_path(project_dir)
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if isinstance(v, list)}
    return {}


def save_memory(memory: dict[str, list[dict]], project_dir: Path | str) -> Path:
    path = memory_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(memory, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def remember(memory: dict[str, list[dict]], node_id: str, user_msg: str, assistant: str) -> None:
    """Acrescenta uma troca à memória do agente, mantendo só as últimas N."""
    history = memory.setdefault(node_id, [])
    history.append({"user": user_msg, "assistant": assistant})
    del history[:-MEMORY_MAX_EXCHANGES]
