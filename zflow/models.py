"""Vocabulário de dados do ZayderFlow: o grafo de agentes como dado.

Dois níveis de validação, de propósito:
- `Graph` (sempre, no save): invariantes estruturais mínimos — o usuário precisa
  poder salvar um grafo pela metade (nó solto, sem aresta) enquanto desenha.
- `validate_runnable` (só no run): o que precisa ser verdade para EXECUTAR —
  grafo não-vazio e acíclico. Erros com mensagem amigável para a UI.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class Node(BaseModel):
    id: str
    type: Literal["text", "coder"] = "text"
    name: str = "Agente"
    model: str = "deepseek/deepseek-v4-flash"  # id litellm
    traits: list[str] = Field(default_factory=list)  # chaves do catálogo (traits.py)
    extra_prompt: str = ""
    include_task: bool = True  # injeta a tarefa original no user message
    x: float = 0.0
    y: float = 0.0
    files: list[str] = Field(default_factory=list)  # só coder: arquivos-alvo sugeridos

    @field_validator("id", "name")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("não pode ser vazio")
        return v


class Edge(BaseModel):
    id: str
    source: str  # node id
    target: str
    label: str = ""
    kind: Literal["normal", "loop"] = "normal"  # "loop" reservado para a fase de ciclos

    @model_validator(mode="after")
    def _no_self_loop(self) -> "Edge":
        if self.source == self.target:
            raise ValueError(f"aresta de '{self.source}' para ele mesmo não é permitida")
        return self


class Graph(BaseModel):
    name: str = "sem título"
    nodes: list[Node] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)
    max_rounds: int = 1  # reservado para ciclos (fase 2)
    cost_ceiling_usd: float | None = 2.0
    approve_coder: bool = True  # gate humano antes de cada nó codador

    @model_validator(mode="after")
    def _structural(self) -> "Graph":
        ids = [n.id for n in self.nodes]
        if len(ids) != len(set(ids)):
            raise ValueError("ids de nós repetidos")
        known = set(ids)
        seen_pairs: set[tuple[str, str]] = set()
        for e in self.edges:
            if e.source not in known or e.target not in known:
                raise ValueError(f"aresta {e.id} aponta para nó inexistente")
            pair = (e.source, e.target)
            if pair in seen_pairs:
                raise ValueError(f"aresta duplicada de {e.source} para {e.target}")
            seen_pairs.add(pair)
        return self

    def node(self, node_id: str) -> Node:
        for n in self.nodes:
            if n.id == node_id:
                return n
        raise KeyError(node_id)

    def predecessors(self, node_id: str) -> list[Node]:
        """Predecessores na ordem das arestas do grafo (ordem estável de input)."""
        return [self.node(e.source) for e in self.edges if e.target == node_id]


def topo_levels(graph: Graph) -> list[list[Node]]:
    """Níveis topológicos (Kahn): todos os nós de um nível têm predecessores prontos.

    Levanta ValueError com mensagem amigável se o grafo tem ciclo.
    """
    indeg = {n.id: 0 for n in graph.nodes}
    for e in graph.edges:
        indeg[e.target] += 1
    levels: list[list[Node]] = []
    ready = [n for n in graph.nodes if indeg[n.id] == 0]
    placed = 0
    while ready:
        levels.append(ready)
        placed += len(ready)
        nxt: list[Node] = []
        ready_ids = {n.id for n in ready}
        for e in graph.edges:
            if e.source in ready_ids:
                indeg[e.target] -= 1
                if indeg[e.target] == 0:
                    nxt.append(graph.node(e.target))
        ready = nxt
    if placed != len(graph.nodes):
        cyclic = sorted(nid for nid, d in indeg.items() if d > 0)
        names = ", ".join(graph.node(nid).name for nid in cyclic[:4])
        raise ValueError(
            f"o grafo tem um ciclo envolvendo: {names}. "
            "Ciclos ainda não são suportados — remova a seta que volta."
        )
    return levels


def validate_runnable(graph: Graph) -> None:
    """O que precisa ser verdade para EXECUTAR o grafo. Erros amigáveis p/ UI."""
    if not graph.nodes:
        raise ValueError("o grafo está vazio — arraste pelo menos um agente para o canvas")
    topo_levels(graph)  # levanta em ciclo


class NodeOutput(BaseModel):
    node_id: str
    text: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    duration_s: float = 0.0
    commit_sha: str | None = None  # só coder
    error: str = ""
    skipped: bool = False


class RunReport(BaseModel):
    task: str = ""
    outputs: dict[str, NodeOutput] = Field(default_factory=dict)
    cost_usd_total: float = 0.0
    completed: bool = False
    aborted: bool = False
