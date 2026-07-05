"""Nó codador: wrapper da API Python do Aider (mesmo padrão do Zayder).

Diferença para o Zayder: aqui não existe TaskSpec/smoke — a instrução do nó é
o que os predecessores mandaram (ex.: a especificação do Arquiteto). A saída do
nó é a resposta do aider + o diff-stat do commit, que é o que os sucessores
leem (ex.: um Revisor dá parecer sobre o que foi feito).
"""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

from .config import Settings
from .models import Node, NodeOutput
from .pricing import cost_for


def git_head_sha(project_dir: Path) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(project_dir), capture_output=True, text=True, timeout=30,
        )
    except OSError:
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def is_git_repo(project_dir: Path) -> bool:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(project_dir), capture_output=True, text=True, timeout=30,
        )
    except OSError:
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def diff_stat(project_dir: Path, before: str | None, after: str | None) -> str:
    if not after or before == after:
        return ""
    rng = f"{before}..{after}" if before else after
    try:
        proc = subprocess.run(
            ["git", "diff", "--stat", rng],
            cwd=str(project_dir), capture_output=True, text=True, timeout=30,
        )
    except OSError:
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


_LITTER_SKIP_DIRS = {".git", ".zflow", ".zayder", "__pycache__", ".venv", "venv", "node_modules"}


def _project_files(project: Path) -> set[Path]:
    out: set[Path] = set()
    for p in project.rglob("*"):
        if p.is_file() and not any(part in _LITTER_SKIP_DIRS for part in p.relative_to(project).parts):
            out.add(p)
    return out


def clean_coder_litter(project: Path, before: set[Path]) -> list[str]:
    """Remove arquivos-lixo do Coder: modelos baratos às vezes grudam prosa antes
    do nome do arquivo e o aider cria um arquivo com ESSE nome — com espaços.
    Arquivo NOVO com espaço no nome é lixo e é removido (disco + git)."""
    removed: list[str] = []
    for path in _project_files(project) - before:
        if " " in path.name:
            tracked = subprocess.run(
                ["git", "rm", "-f", "--quiet", "--", str(path.relative_to(project))],
                cwd=str(project), capture_output=True, text=True, timeout=30,
            )
            if tracked.returncode != 0:
                try:
                    path.unlink()
                except OSError:
                    continue
            removed.append(path.name)
    return removed


def run_with_timeout(fn, timeout_s: float):
    """Roda fn() numa thread daemon. Devolve (resultado, timed_out). Propaga a
    exceção de fn se ela terminar com erro dentro do prazo."""
    holder: dict = {}

    def _work() -> None:
        try:
            holder["result"] = fn()
        except BaseException as exc:  # noqa: BLE001 - repassado ao chamador
            holder["error"] = exc

    worker = threading.Thread(target=_work, daemon=True)
    worker.start()
    worker.join(timeout_s)
    if worker.is_alive():
        return None, True
    if "error" in holder:
        raise holder["error"]
    return holder.get("result"), False


def build_coder_instruction(node: Node, user_msg: str) -> str:
    parts = []
    if node.extra_prompt.strip():
        parts.append(node.extra_prompt.strip())
    parts.append(user_msg)
    parts.append(
        "Implemente as mudanças descritas acima nos arquivos do projeto. "
        "Crie arquivos novos quando fizer sentido. Não invente dependências que "
        "não existem no projeto."
    )
    return "\n\n".join(parts)


def run_coder_node(node: Node, user_msg: str, settings: Settings) -> NodeOutput:
    project = settings.resolved_project_dir()
    if not is_git_repo(project):
        return NodeOutput(
            node_id=node.id,
            error="o diretório do projeto não é um repositório git — o agente codador "
                  f"precisa de git para commitar ({project}). Rode `git init` lá.",
        )

    # import tardio: aider é pesado e os testes injetam coder_fn fake
    from aider.coders import Coder
    from aider.io import InputOutput
    from aider.models import Model

    fnames = []
    for rel in node.files:
        target = project / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        fnames.append(str(target))
    before_files = _project_files(project)
    before_sha = git_head_sha(project)

    model = Model(node.model)
    try:
        # timeout por requisição no litellm: chamada presa à API erra em vez de
        # pendurar a thread pra sempre
        model.extra_params = {
            **(getattr(model, "extra_params", None) or {}),
            "timeout": settings.node_timeout_s,
        }
    except Exception:  # noqa: BLE001 - se o aider mudar a API, segue sem isso
        pass

    # pretty=False/stream=False: sem rich ao vivo (consoles cp1252 morrem com
    # UnicodeEncodeError no streaming); whole: modelos baratos corrompem a linha
    # do filename no formato diff; map_tokens=0: o repo map é o grande dreno de
    # tokens e a instrução já traz o contexto dos predecessores
    coder = Coder.create(
        main_model=model,
        fnames=fnames,
        io=InputOutput(yes=True, pretty=False, fancy_input=False),
        edit_format="whole",
        auto_commits=True,
        use_git=True,
        stream=False,
        map_tokens=0,
    )

    started = time.monotonic()
    instruction = build_coder_instruction(node, user_msg)
    text, timed_out = run_with_timeout(
        lambda: coder.run(instruction) or "", settings.node_timeout_s + 30
    )
    duration = time.monotonic() - started
    clean_coder_litter(project, before_files)
    after_sha = git_head_sha(project)

    tin = int(getattr(coder, "message_tokens_sent", 0) or 0)
    tout = int(getattr(coder, "message_tokens_received", 0) or 0)
    cost = cost_for(node.model, tin, tout) or float(getattr(coder, "total_cost", 0.0) or 0.0)

    if timed_out:
        return NodeOutput(
            node_id=node.id,
            error=f"timeout: a chamada ao modelo passou de {settings.node_timeout_s}s "
                  "e foi abandonada. O provedor pode estar lento ou fora do ar.",
            tokens_in=tin, tokens_out=tout, cost_usd=cost,
            duration_s=round(duration, 2), commit_sha=after_sha,
        )

    stat = diff_stat(project, before_sha, after_sha)
    parts = [text or "(o aider não retornou texto)"]
    if stat:
        parts.append("## Mudanças commitadas\n```\n" + stat + "\n```")
    elif after_sha == before_sha:
        parts.append("(nenhum commit novo foi criado — o modelo pode não ter editado nada)")
    return NodeOutput(
        node_id=node.id,
        text="\n\n".join(parts),
        tokens_in=tin, tokens_out=tout, cost_usd=cost,
        duration_s=round(duration, 2), commit_sha=after_sha,
    )
