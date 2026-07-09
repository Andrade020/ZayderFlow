"""Expansão de loops: a seta 🔁 (kind="loop") vira cópias do trecho repetido.

O executor só entende DAGs — e continua assim. Antes de rodar, cada seta de
repetição S→T (de um nó posterior de volta a um anterior) é "desenrolada":

- o CORPO do loop = os nós entre T e S (inclusive), pelas setas normais;
- para cada rodada extra, o corpo é clonado (ids "n3~2", "n3~3", …) e a saída
  do fim da rodada anterior alimenta o começo da próxima;
- cada clone aponta para o personagem original (persona), então a rodada 2
  LEMBRA o que foi dito na rodada 1 — é o mesmo mecanismo do "duplicar
  personagem", só que automático;
- setas que saem do corpo para o resto do fluxo passam a sair da ÚLTIMA
  rodada (quem vem depois vê o resultado final, não o intermediário).

A UI nunca vê o grafo expandido: eventos de "n3~2" são mapeados de volta para
a caixinha n3 (o nó "reacende" a cada rodada).
"""

from __future__ import annotations

from .models import Edge, Graph, persona_key

MAX_ROUNDS = 30  # trava de segurança contra loop gigante por engano


def _reach(adj: dict[str, list[str]], start: str) -> set[str]:
    seen: set[str] = set()
    stack = [start]
    while stack:
        cur = stack.pop()
        for nxt in adj.get(cur, ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def expand_loops(graph: Graph) -> Graph:
    """Grafo desenhado (com setas 🔁) → DAG executável (só arestas normais).

    Levanta ValueError com mensagem amigável se um loop está mal desenhado.
    """
    loops = [e for e in graph.edges if e.kind == "loop"]
    if not loops:
        return graph

    normal = [e for e in graph.edges if e.kind != "loop"]
    adj: dict[str, list[str]] = {}
    radj: dict[str, list[str]] = {}
    for e in normal:
        adj.setdefault(e.source, []).append(e.target)
        radj.setdefault(e.target, []).append(e.source)
    by_id = {n.id: n for n in graph.nodes}

    # corpo de cada loop; corpos não podem se sobrepor (um loop de cada vez)
    bodies: list[tuple[Edge, set[str]]] = []
    used: set[str] = set()
    for le in loops:
        s_id, t_id = le.source, le.target
        if s_id == t_id:
            body = {s_id}
        else:
            forward = _reach(adj, t_id)  # quem T alcança
            if s_id not in forward:
                raise ValueError(
                    f"a seta de repetição de '{by_id[s_id].name}' para "
                    f"'{by_id[t_id].name}' precisa voltar para um ponto ANTERIOR "
                    "do fluxo (as setas normais devem levar do destino de volta à origem)"
                )
            backward = _reach(radj, s_id)  # quem alcança S
            body = (forward | {t_id}) & (backward | {s_id})
        if body & used:
            raise ValueError(
                "duas setas de repetição não podem compartilhar o mesmo trecho "
                "do fluxo — separe os loops ou use uma só"
            )
        used |= body
        bodies.append((le, body))

    body_of: dict[str, int] = {}
    for i, (_, body) in enumerate(bodies):
        for nid in body:
            body_of[nid] = i

    new_nodes = list(graph.nodes)
    extra_edges: list[Edge] = []
    last_copy: dict[str, str] = {}  # nó do corpo -> id da cópia da última rodada

    for le, body in bodies:
        rounds = max(2, min(MAX_ROUNDS, le.rounds))
        internal = [e for e in normal if e.source in body and e.target in body]
        for k in range(2, rounds + 1):
            for nid in body:
                base = by_id[nid]
                new_nodes.append(base.model_copy(update={
                    "id": f"{nid}~{k}",
                    "name": f"{base.name} (rodada {k})",
                    # clones são o MESMO personagem: rodada k lembra as anteriores
                    "persona": persona_key(base),
                }))
            for e in internal:
                extra_edges.append(Edge(id=f"{e.id}~{k}", source=f"{e.source}~{k}",
                                        target=f"{e.target}~{k}", label=e.label))
            prev = le.source if k == 2 else f"{le.source}~{k - 1}"
            extra_edges.append(Edge(id=f"{le.id}~volta{k}", source=prev,
                                    target=f"{le.target}~{k}",
                                    label=le.label or "próxima rodada"))
        for nid in body:
            last_copy[nid] = f"{nid}~{rounds}"

    # setas que SAEM do corpo para fora passam a sair da última rodada
    new_edges: list[Edge] = []
    for e in normal:
        src_body = body_of.get(e.source)
        if src_body is not None and body_of.get(e.target) != src_body:
            new_edges.append(e.model_copy(update={"source": last_copy[e.source]}))
        else:
            new_edges.append(e)
    new_edges.extend(extra_edges)

    return graph.model_copy(update={"nodes": new_nodes, "edges": new_edges})


def base_id(node_id: str) -> str:
    """'n3~2' → 'n3' (a caixinha desenhada por trás de uma cópia de rodada)."""
    return node_id.split("~", 1)[0]
