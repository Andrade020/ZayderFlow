"""Catálogo de personalidades (traits) e a montagem do system prompt de cada nó.

Cada trait é uma frase de instrução pronta; o usuário liga/desliga por chips na
UI e pode complementar com texto livre (extra_prompt). O prompt final é uma
concatenação de blocos — simples de prever, simples de mostrar na UI.
"""

from __future__ import annotations

from .models import Node

TRAITS: dict[str, str] = {
    "rigoroso": (
        "Você é extremamente rigoroso: aponta toda inconsistência, número sem fonte "
        "e passo sem justificativa."
    ),
    "critico": (
        "Você é um crítico severo: seu papel é achar problemas, riscos e pontos "
        "fracos — não elogie."
    ),
    "elogiador": (
        "Você destaca os pontos fortes e o que merece ser mantido; seja específico "
        "no elogio."
    ),
    "criterioso": (
        "Você pondera prós e contras com critérios explícitos antes de concluir."
    ),
    "conciso": "Responda de forma curta e direta, sem preâmbulos.",
    "criativo": "Proponha ângulos não-óbvios e alternativas que ninguém sugeriu.",
    "cetico": "Questione premissas; assuma que a informação recebida pode estar errada.",
    "didatico": "Explique como para alguém inteligente mas leigo no assunto.",
}

# rótulos bonitos para a UI (a chave é ascii para viver em JSON/ids sem susto)
TRAIT_LABELS: dict[str, str] = {
    "rigoroso": "Rigoroso",
    "critico": "Crítico",
    "elogiador": "Elogiador",
    "criterioso": "Criterioso",
    "conciso": "Conciso",
    "criativo": "Criativo",
    "cetico": "Cético",
    "didatico": "Didático",
}


def build_system_prompt(node: Node) -> str:
    if node.prompt_override.strip():
        return node.prompt_override.strip()
    parts = [
        f'Você é "{node.name}", um agente num fluxo com vários agentes de IA.',
    ]
    for key in node.traits:
        phrase = TRAITS.get(key)
        if phrase:
            parts.append(phrase)
    extra = node.extra_prompt.strip()
    if extra:
        parts.append(extra)
    parts.append(
        "Você receberá uma tarefa e, possivelmente, mensagens de outros agentes. "
        "Responda em português. Emita SOMENTE a sua contribuição, sem repetir as "
        "mensagens recebidas."
    )
    return "\n".join(parts)
