"""Implementações de nó: o "corpo" de cada tipo de caixinha do canvas.

Nó de texto = litellm puro (recebe texto, responde texto). O custo e os tokens
são extraídos DIRETO da resposta (usage_from) — atribuição por nó, sem depender
de logger global. O nó codador (Aider) chega na F4.
"""

from __future__ import annotations

import time

from .config import Settings
from .models import Node, NodeOutput
from .pricing import cost_for, usage_from
from .traits import build_system_prompt


def build_user_message(task: str, node: Node, received: list[tuple[str, str]]) -> str:
    """Monta o user message por convenção fixa (sem templating no MVP).

    `received` = [(nome do predecessor, texto)] na ordem das arestas do grafo.
    """
    parts: list[str] = []
    if node.include_task and task.strip():
        parts.append(f"## Tarefa\n{task.strip()}")
    if received:
        blocks = [f"### de {name}:\n{text}" for name, text in received]
        parts.append("## Mensagens recebidas\n" + "\n\n".join(blocks))
    return "\n\n".join(parts) or "(sem tarefa nem mensagens — contribua com o que souber)"


def run_text_node(node: Node, user_msg: str, settings: Settings) -> NodeOutput:
    """Chama o modelo do nó com o system prompt da personalidade dele."""
    import litellm

    started = time.monotonic()
    response = litellm.completion(
        model=node.model,
        messages=[
            {"role": "system", "content": build_system_prompt(node)},
            {"role": "user", "content": user_msg},
        ],
        timeout=settings.node_timeout_s,
    )
    duration = time.monotonic() - started
    text = (response.choices[0].message.content or "").strip()
    tin, tout = usage_from(response)
    return NodeOutput(
        node_id=node.id,
        text=text,
        tokens_in=tin,
        tokens_out=tout,
        cost_usd=cost_for(node.model, tin, tout),
        duration_s=round(duration, 2),
    )
