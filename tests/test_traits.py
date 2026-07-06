from __future__ import annotations

from zflow.models import Node
from zflow.traits import TRAIT_LABELS, TRAITS, build_system_prompt


def test_every_trait_has_label():
    assert set(TRAITS) == set(TRAIT_LABELS)


def test_prompt_contains_name_and_traits():
    node = Node(id="n1", name="Analista Rigoroso", traits=["rigoroso", "conciso"])
    prompt = build_system_prompt(node)
    assert '"Analista Rigoroso"' in prompt
    assert TRAITS["rigoroso"] in prompt
    assert TRAITS["conciso"] in prompt


def test_prompt_includes_extra_and_final_rule():
    node = Node(id="n1", name="X", extra_prompt="Produza um parecer único.")
    prompt = build_system_prompt(node)
    assert "Produza um parecer único." in prompt
    assert "SOMENTE a sua contribuição" in prompt


def test_unknown_trait_is_ignored():
    node = Node(id="n1", name="X", traits=["nao-existe"])
    prompt = build_system_prompt(node)
    assert "nao-existe" not in prompt


def test_custom_trait_roundtrip():
    from zflow.traits import (
        add_custom_trait, catalog, delete_custom_trait, load_custom_traits, trait_prompt,
    )

    key = add_custom_trait("Sarcástico", "Responda com ironia leve.")
    assert key == "sarcastico"  # acento removido no slug
    assert trait_prompt(key) == "Responda com ironia leve."
    # persistiu no arquivo: recarregar mantém
    load_custom_traits()
    assert trait_prompt(key) == "Responda com ironia leve."
    entry = next(t for t in catalog() if t["key"] == key)
    assert entry["custom"] is True and entry["label"] == "Sarcástico"
    assert delete_custom_trait(key) is True
    assert trait_prompt(key) is None


def test_custom_trait_key_never_collides_with_builtin():
    from zflow.traits import add_custom_trait

    key = add_custom_trait("Crítico", "Outra definição de crítico.")
    assert key != "critico" and key.startswith("critico")


def test_builtin_traits_cannot_be_deleted():
    from zflow.traits import delete_custom_trait

    assert delete_custom_trait("rigoroso") is False


def test_custom_trait_used_in_system_prompt():
    from zflow.traits import add_custom_trait

    key = add_custom_trait("Pirata", "Fale como um pirata.")
    node = Node(id="n1", name="X", traits=[key])
    assert "Fale como um pirata." in build_system_prompt(node)


def test_prompt_override_replaces_everything():
    node = Node(id="n1", name="X", traits=["rigoroso"],
                extra_prompt="ignorado", prompt_override="Você é um pirata. Responda em versos.")
    prompt = build_system_prompt(node)
    assert prompt == "Você é um pirata. Responda em versos."
    assert TRAITS["rigoroso"] not in prompt
    # limpar o override volta ao automático (traits preservados)
    node.prompt_override = ""
    assert TRAITS["rigoroso"] in build_system_prompt(node)
