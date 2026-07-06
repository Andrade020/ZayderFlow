"""Parser de blocos de código nas respostas dos agentes de texto.

Extrai blocos cercados (```), tenta descobrir o nome do arquivo (na linha do
fence, no comentário da 1ª linha ou na linha de prosa logo antes do bloco) e
salva no diretório do projeto — é o que transforma "aqui está o hello_world.py"
em um arquivo de verdade, executável pelo painel de Arquivos.
"""

from __future__ import annotations

import re
from pathlib import Path

FENCE_RE = re.compile(r"```([^\n`]*)\n(.*?)```", re.S)
# nome de arquivo plausível: letras/números/_-. com extensão curta
FILENAME_RE = re.compile(r"(?<![\w./\\-])([\w\-./]+\.[A-Za-z][A-Za-z0-9]{0,9})(?![\w./\\-])")
COMMENT_FILENAME_RE = re.compile(r"^\s*(?:#|//|--|<!--)\s*([\w\-./]+\.[A-Za-z][A-Za-z0-9]{0,9})")

EXT_BY_LANG = {
    "python": ".py", "py": ".py",
    "javascript": ".js", "js": ".js",
    "typescript": ".ts", "ts": ".ts",
    "html": ".html", "css": ".css",
    "json": ".json", "yaml": ".yaml", "yml": ".yml",
    "bash": ".sh", "sh": ".sh", "sql": ".sql",
    "markdown": ".md", "md": ".md", "text": ".txt", "txt": ".txt",
}

# extensões que nunca fazem sentido como "nome de arquivo" detectado em prosa
_BAD_EXTS = {".com", ".org", ".net", ".br", ".io", ".ai"}


def _plausible(name: str) -> bool:
    # pedaço de URL não é nome de arquivo ("https://x.com/a.py" -> "//x.com/a.py")
    if name.startswith(("http", "www.", "/", "\\")) or "//" in name or ".." in name:
        return False
    return Path(name).suffix.lower() not in _BAD_EXTS


def _find_filename(info: str, code: str, prose_before: str) -> str | None:
    """Nome do arquivo, em ordem de confiança: fence -> comentário -> prosa."""
    for m in FILENAME_RE.finditer(info):
        if _plausible(m.group(1)):
            return m.group(1)
    first_line = code.lstrip("\n").split("\n", 1)[0]
    m = COMMENT_FILENAME_RE.match(first_line)
    if m and _plausible(m.group(1)):
        return m.group(1)
    for line in reversed([ln for ln in prose_before.splitlines() if ln.strip()][-2:]):
        for m in FILENAME_RE.finditer(line):
            if _plausible(m.group(1)):
                return m.group(1)
    return None


def extract_code_blocks(text: str) -> list[dict]:
    """[{lang, filename|None, code}] para cada bloco cercado da resposta."""
    blocks: list[dict] = []
    last_end = 0
    for m in FENCE_RE.finditer(text or ""):
        info = m.group(1).strip()
        code = m.group(2)
        lang = ""
        tokens = info.split()
        if tokens and "." not in tokens[0]:
            lang = tokens[0].lower()
        prose_before = text[last_end:m.start()]
        filename = _find_filename(info, code, prose_before)
        blocks.append({"lang": lang, "filename": filename, "code": code.rstrip("\n") + "\n"})
        last_end = m.end()
    return blocks


def suggest_filename(block: dict, fallback_stem: str, index: int) -> str:
    if block.get("filename"):
        return block["filename"]
    ext = EXT_BY_LANG.get(block.get("lang", ""), ".txt")
    stem = re.sub(r"[^\w\-]+", "_", fallback_stem.strip().lower()).strip("_") or "saida"
    suffix = f"_{index + 1}" if index else ""
    return f"{stem}{suffix}{ext}"


def resolve_within(project_dir: Path, rel: str) -> Path:
    """Resolve um caminho relativo GARANTINDO que fica dentro do projeto."""
    rel = (rel or "").strip().replace("\\", "/")
    if not rel:
        raise ValueError("nome de arquivo vazio")
    project = project_dir.resolve()
    target = (project / rel).resolve()
    try:
        target.relative_to(project)
    except ValueError:
        raise ValueError(f"caminho fora do projeto: {rel}")
    return target


def save_named_blocks(text: str, project_dir: Path) -> list[str]:
    """Salva os blocos que têm nome DETECTÁVEL (auto-save do node.save_files).

    Blocos sem nome não são salvos sozinhos — para esses existe o botão manual
    no drawer, onde o usuário escolhe o nome vendo o conteúdo.
    """
    saved: list[str] = []
    for block in extract_code_blocks(text):
        if not block["filename"]:
            continue
        try:
            target = resolve_within(project_dir, block["filename"])
        except ValueError:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(block["code"], encoding="utf-8")
        saved.append(str(target.relative_to(project_dir.resolve())).replace("\\", "/"))
    return saved
