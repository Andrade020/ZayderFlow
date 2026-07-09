"""Configuração do ZayderFlow.

Config do projeto vive em <project_dir>/.zflow/config.json. Chaves de API NUNCA
ficam aí: vêm do ambiente ou de ~/.zflow/keys.json — com fallback para
~/.zayder/keys.json, para quem já usa o Zayder não precisar configurar de novo.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import BaseModel

CONFIG_DIR_NAME = ".zflow"
CONFIG_FILE_NAME = "config.json"


class Settings(BaseModel):
    project_dir: Path = Path(".")
    default_text_model: str = "deepseek/deepseek-v4-flash"
    default_coder_model: str = "deepseek/deepseek-v4-flash"
    node_timeout_s: int = 180  # teto por chamada de nó (texto ou coder)
    max_workers: int = 4  # nós texto em paralelo por nível
    max_attempts: int = 2  # tentativas por nó em falha de API/timeout (1 = sem retry)
    # salvar automaticamente blocos de código nomeados de TODOS os agentes de
    # texto, mesmo sem o checkbox 💾 por agente (opção global do ⚙️ Setup)
    auto_save_code: bool = False

    def resolved_project_dir(self) -> Path:
        return self.project_dir.expanduser().resolve()

    def config_path(self) -> Path:
        return self.resolved_project_dir() / CONFIG_DIR_NAME / CONFIG_FILE_NAME

    def save(self) -> Path:
        path = self.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.model_dump(mode="json"), indent=2), encoding="utf-8")
        return path


def load_settings(project_dir: Path | str = ".") -> Settings:
    """Carrega a config do projeto; ausente vira defaults com esse project_dir."""
    project_dir = Path(project_dir).expanduser().resolve()
    path = project_dir / CONFIG_DIR_NAME / CONFIG_FILE_NAME
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        data["project_dir"] = str(project_dir)
        return Settings.model_validate(data)
    return Settings(project_dir=project_dir)


def load_api_keys() -> dict[str, str]:
    """Aplica chaves de ~/.zflow/keys.json (fallback: ~/.zayder/keys.json) ao ambiente.

    Variáveis já definidas no ambiente têm precedência e não são sobrescritas.
    Retorna o que foi efetivamente aplicado.
    """
    applied: dict[str, str] = {}
    for dirname in (CONFIG_DIR_NAME, ".zayder"):
        keys_path = Path.home() / dirname / "keys.json"
        if not keys_path.is_file():
            continue
        data = json.loads(keys_path.read_text(encoding="utf-8-sig"))
        for name, value in data.items():
            if name not in os.environ and isinstance(value, str) and value:
                os.environ[name] = value
                applied[name] = value
    return applied


def save_api_key(name: str, value: str) -> Path:
    """Grava uma chave em ~/.zflow/keys.json (fora de qualquer repositório)."""
    keys_path = Path.home() / CONFIG_DIR_NAME / "keys.json"
    keys_path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, str] = {}
    if keys_path.is_file():
        data = json.loads(keys_path.read_text(encoding="utf-8-sig"))
    data[name] = value
    keys_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.environ[name] = value
    return keys_path
