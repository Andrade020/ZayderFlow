"""Catálogo de personalidades (traits) e a montagem do system prompt de cada nó.

Cada trait é uma frase de instrução pronta; o usuário liga/desliga por chips na
UI e pode complementar com texto livre (extra_prompt). O prompt final é uma
concatenação de blocos — simples de prever, simples de mostrar na UI.

Além das embutidas, o usuário cadastra as SUAS em ~/.zflow/traits.json —
globais (valem em qualquer projeto), com chave própria para não colidir com as
embutidas.
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

from .models import Node

# monkeypatchável nos testes
CUSTOM_TRAITS_PATH = Path.home() / ".zflow" / "traits.json"

# {key: {"label": ..., "prompt": ...}} — carregado no startup do servidor
_custom: dict[str, dict] = {}

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


def _slug(label: str) -> str:
    s = unicodedata.normalize("NFD", label)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^\w]+", "_", s.lower()).strip("_")
    return s or "personalidade"


def load_custom_traits() -> dict[str, dict]:
    """Carrega ~/.zflow/traits.json para o registro em memória."""
    _custom.clear()
    if CUSTOM_TRAITS_PATH.is_file():
        data = json.loads(CUSTOM_TRAITS_PATH.read_text(encoding="utf-8-sig"))
        for key, val in data.items():
            if isinstance(val, dict) and val.get("label") and val.get("prompt"):
                _custom[key] = {"label": val["label"], "prompt": val["prompt"]}
    return dict(_custom)


def _save_custom_file() -> None:
    CUSTOM_TRAITS_PATH.parent.mkdir(parents=True, exist_ok=True)
    CUSTOM_TRAITS_PATH.write_text(
        json.dumps(_custom, indent=2, ensure_ascii=False), encoding="utf-8")


def add_custom_trait(label: str, prompt: str) -> str:
    """Cadastra uma personalidade nova; retorna a chave dela."""
    label, prompt = label.strip(), prompt.strip()
    if not label or not prompt:
        raise ValueError("nome e instrução são obrigatórios")
    key = _slug(label)
    while key in TRAITS or (key in _custom and _custom[key]["label"] != label):
        key += "_2"  # não colide com embutida nem com outra custom
    _custom[key] = {"label": label, "prompt": prompt}
    _save_custom_file()
    return key


def delete_custom_trait(key: str) -> bool:
    """Remove uma personalidade SUA (as embutidas não podem ser apagadas)."""
    if key not in _custom:
        return False
    del _custom[key]
    _save_custom_file()
    return True


def trait_prompt(key: str) -> str | None:
    if key in _custom:
        return _custom[key]["prompt"]
    return TRAITS.get(key)


def catalog() -> list[dict]:
    """Embutidas + customizadas, no formato da UI."""
    items = [
        {"key": k, "label": TRAIT_LABELS.get(k, k), "prompt": v, "custom": False}
        for k, v in TRAITS.items()
    ]
    items += [
        {"key": k, "label": v["label"], "prompt": v["prompt"], "custom": True}
        for k, v in sorted(_custom.items())
    ]
    return items


def build_system_prompt(node: Node) -> str:
    if node.prompt_override.strip():
        return node.prompt_override.strip()
    parts = [
        f'Você é "{node.name}", um agente num fluxo com vários agentes de IA.',
    ]
    for key in node.traits:
        phrase = trait_prompt(key)
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
