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
