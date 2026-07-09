from __future__ import annotations

import pytest

from zflow.codeblocks import (
    extract_code_blocks,
    resolve_within,
    save_named_blocks,
    suggest_filename,
)


def test_filename_from_fence_info():
    text = "aqui está:\n```python hello_world.py\nprint('oi')\n```"
    blocks = extract_code_blocks(text)
    assert len(blocks) == 1
    assert blocks[0]["lang"] == "python"
    assert blocks[0]["filename"] == "hello_world.py"
    assert blocks[0]["code"] == "print('oi')\n"


def test_filename_from_first_line_comment():
    text = "```python\n# jogo/snake.py\nx = 1\n```"
    b = extract_code_blocks(text)[0]
    assert b["filename"] == "jogo/snake.py"


def test_filename_from_prose_before_block():
    text = "Crie o arquivo `util.py` com o conteúdo:\n```python\ndef f(): pass\n```"
    b = extract_code_blocks(text)[0]
    assert b["filename"] == "util.py"


def test_no_filename_and_urls_ignored():
    text = "veja https://exemplo.com/x.py e rode:\n```python\nprint(1)\n```"
    b = extract_code_blocks(text)[0]
    # a URL não pode virar nome de arquivo
    assert b["filename"] is None or "exemplo" not in b["filename"]


def test_multiple_blocks():
    text = "```python a.py\n1\n```\ntexto\n```js b.js\n2\n```"
    blocks = extract_code_blocks(text)
    assert [b["filename"] for b in blocks] == ["a.py", "b.js"]


def test_suggest_filename_fallback_by_lang():
    b = {"lang": "python", "filename": None, "code": "x\n"}
    assert suggest_filename(b, "Gerador Criativo", 0) == "gerador_criativo.py"
    assert suggest_filename(b, "Gerador Criativo", 1) == "gerador_criativo_2.py"
    assert suggest_filename({"lang": "", "filename": None, "code": ""}, "X", 0) == "x.txt"


def test_resolve_within_blocks_escape(tmp_path):
    with pytest.raises(ValueError):
        resolve_within(tmp_path, "../fora.py")
    with pytest.raises(ValueError):
        resolve_within(tmp_path, "")
    ok = resolve_within(tmp_path, "sub/dentro.py")
    assert str(ok).startswith(str(tmp_path.resolve()))


def test_save_named_blocks_writes_only_named(tmp_path):
    text = (
        "```python hello.py\nprint('oi')\n```\n"
        "```python\nsem_nome = True\n```\n"
        "```python ../escape.py\nmal = True\n```"
    )
    saved = save_named_blocks(text, tmp_path)
    assert saved == ["hello.py"]
    assert (tmp_path / "hello.py").read_text(encoding="utf-8") == "print('oi')\n"
    assert not (tmp_path.parent / "escape.py").exists()


def test_filename_in_prose_a_few_lines_above():
    text = ("**snake.py** — o arquivo principal do jogo.\n"
            "Ele usa apenas a biblioteca padrão.\n\n"
            "```python\nprint('jogo')\n```")
    blocks = extract_code_blocks(text)
    assert blocks[0]["filename"] == "snake.py"


def test_save_files_prompt_rule_added():
    from zflow.models import Node
    from zflow.traits import build_system_prompt

    with_save = build_system_prompt(Node(id="a", name="G", save_files=True))
    without = build_system_prompt(Node(id="a", name="G", save_files=False))
    assert "linha de abertura da cerca" in with_save
    assert "linha de abertura da cerca" not in without
