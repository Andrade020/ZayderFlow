"""Servidor local do ZayderFlow (FastAPI + polling, sem websockets).

Mesmo padrão do Zayder: um FlowManager com `seq` monotônico + lock protege o
estado; o frontend faz GET /api/state?since=N a cada segundo e recebe só os
eventos novos. Localhost, um usuário — polling é suficiente e simples.

Eventos carregam só um preview (~2KB) da saída de cada nó; o texto completo sai
por GET /api/output/{node_id} — cinco analistas × 8KB a cada tick estourariam o
payload do polling.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ValidationError

from .codeblocks import extract_code_blocks, resolve_within, suggest_filename
from .config import Settings, load_api_keys, load_settings, save_api_key
from .executor import CoderFn, GraphExecutor, TextFn
from .gallery import list_templates, load_template
from .looping import base_id, expand_loops
from .models import Graph, Node, RunReport, persona_key, validate_runnable
from .presets import all_presets
from .pricing import PRICES_PER_M, cost_for, opus_equiv_usd
from .store import load_graph, load_memory, save_graph, save_memory
from .traits import add_custom_trait, catalog, delete_custom_trait, load_custom_traits

WEBAPP_DIR = Path(__file__).parent / "webapp"

BUSY_PHASES = ("running", "waiting_approval")

_FILES_SKIP_DIRS = {".git", ".zflow", ".zayder", "__pycache__", ".venv", "venv",
                    "node_modules", ".pytest_cache", ".ruff_cache", "dist", "build",
                    "*.egg-info"}
FILE_VIEW_MAX_CHARS = 40_000
RUN_OUTPUT_MAX_CHARS = 20_000
INPUT_CONTEXT_MAX_CHARS = 4_000  # contexto mostrado no cartão "responda você"

# provedores comuns no ⚙️ Setup (qualquer outra variável pode ser salva também)
KNOWN_API_KEYS = [
    "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY", "OPENROUTER_API_KEY", "MISTRAL_API_KEY", "XAI_API_KEY",
]

# código gerado importa bibliotecas que não estão no venv do zflow; ao rodar,
# detectamos o ModuleNotFoundError e oferecemos instalar o pacote certo
MISSING_MODULE_RE = re.compile(r"ModuleNotFoundError: No module named '([\w\.]+)'")
MODULE_TO_PACKAGE = {
    "cv2": "opencv-python", "PIL": "pillow", "Image": "pillow",
    "sklearn": "scikit-learn", "skimage": "scikit-image",
    "yaml": "pyyaml", "bs4": "beautifulsoup4", "dotenv": "python-dotenv",
    "Crypto": "pycryptodome", "fitz": "pymupdf", "serial": "pyserial",
    "dateutil": "python-dateutil", "docx": "python-docx", "pptx": "python-pptx",
    "github": "PyGithub", "telegram": "python-telegram-bot", "wx": "wxPython",
    "OpenGL": "PyOpenGL", "win32com": "pywin32", "win32api": "pywin32",
}
PACKAGE_NAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._\-]*[A-Za-z0-9])?(?:\[[A-Za-z0-9,_\-]+\])?$")


def _pip_install(package: str) -> subprocess.CompletedProcess:
    """pip install no MESMO venv que roda os arquivos (monkeypatchável nos testes)."""
    return subprocess.run(
        [sys.executable, "-m", "pip", "install", package],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=300,
    )


def _open_path(path: Path) -> None:
    """Abre a pasta no gerenciador de arquivos do SO (monkeypatchável nos testes)."""
    norm = os.path.normpath(str(path))
    if sys.platform == "win32":
        try:
            subprocess.Popen(["explorer", norm])  # traz a janela pra frente
        except OSError:
            os.startfile(norm)  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", norm])
    else:
        subprocess.Popen(["xdg-open", norm])


class FlowManager:
    """Estado central em memória. Fases: idle → running ⇄ waiting_approval → done | error."""

    def __init__(self, settings: Settings, text_fn: TextFn | None = None,
                 coder_fn: CoderFn | None = None):
        self.settings = settings
        self.text_fn = text_fn
        self.coder_fn = coder_fn
        self.lock = threading.Lock()
        self.phase = "idle"
        self.seq = 0
        self.events: list[dict] = []
        self.graph: Graph = load_graph(settings.resolved_project_dir())
        self.memory: dict[str, list[dict]] = load_memory(settings.resolved_project_dir())
        self.node_status: dict[str, str] = {}  # id -> pending|running|done|error|skipped
        self.report: RunReport | None = None
        self.executor: GraphExecutor | None = None
        self.task = ""
        self.error = ""
        self.pending_approval = ""
        self._decision = "approve"
        self._decision_ready = threading.Event()
        self.tokens_in = 0
        self.tokens_out = 0
        self.by_model: dict[str, list[int]] = {}
        # nós 👤 esperando resposta do usuário: node_id -> {name, question, context, event, response}
        self.pending_inputs: dict[str, dict] = {}
        # feedback do usuário por personagem (persona key) — injetado nas
        # próximas chamadas daquele personagem até ser apagado
        self.feedback: dict[str, list[str]] = {}
        # grafo expandido (loops desenrolados) da execução atual — os eventos
        # usam os ids dele; a UI só conhece o grafo desenhado
        self._exec_graph: Graph | None = None

    # -- eventos ----------------------------------------------------------
    def emit(self, kind: str, **data) -> None:
        with self.lock:
            self.seq += 1
            self.events.append({"seq": self.seq, "kind": kind, **data})

    def record_tokens(self, model: str, tin: int, tout: int) -> None:
        with self.lock:
            self.tokens_in += tin
            self.tokens_out += tout
            pair = self.by_model.setdefault(model, [0, 0])
            pair[0] += tin
            pair[1] += tout

    # -- grafo -------------------------------------------------------------
    def set_graph(self, graph: Graph) -> None:
        if self.phase in BUSY_PHASES:
            raise RuntimeError("não dá para editar o grafo com uma execução em andamento")
        self.graph = graph
        save_graph(graph, self.settings.resolved_project_dir())

    # -- projeto -------------------------------------------------------------
    def switch_project(self, path: str, create: bool = False) -> None:
        """Troca o diretório de destino (o "repo") em tempo de execução.

        Recarrega grafo, config e memória do diretório novo; o estado da
        execução anterior é descartado (o seq segue monotônico).
        """
        if self.phase in BUSY_PHASES:
            raise RuntimeError("não dá para trocar de projeto com uma execução em andamento")
        p = Path(path).expanduser()
        if not p.is_dir():
            if not create:
                raise FileNotFoundError(f"o diretório não existe: {p}")
            p.mkdir(parents=True, exist_ok=True)
        self.settings = load_settings(p)
        self.graph = load_graph(self.settings.resolved_project_dir())
        self.memory = load_memory(self.settings.resolved_project_dir())
        self.phase = "idle"
        self.task = ""
        self.error = ""
        self.pending_approval = ""
        self.report = None
        self.node_status = {}
        with self.lock:
            self.events = []
            self.tokens_in = 0
            self.tokens_out = 0
            self.by_model = {}
            self.feedback = {}  # feedback é por personagem — projeto novo, elenco novo

    # -- memória -------------------------------------------------------------
    def clear_memory(self, node_id: str | None = None) -> None:
        if node_id is None:
            self.memory.clear()
        else:
            # instâncias duplicadas compartilham a memória do personagem original
            node = next((n for n in self.graph.nodes if n.id == node_id), None)
            self.memory.pop(persona_key(node) if node else node_id, None)
        save_memory(self.memory, self.settings.resolved_project_dir())

    # -- input humano (nó 👤) -------------------------------------------------
    def _ask_input(self, node: Node, question: str, context: str) -> str | None:
        """Chamado pela thread do executor: pausa o nó até o usuário responder."""
        ready = threading.Event()
        entry = {"node_id": node.id, "name": node.name, "question": question,
                 "context": context[:INPUT_CONTEXT_MAX_CHARS],
                 "event": ready, "response": None}
        with self.lock:
            self.pending_inputs[node.id] = entry
        self.emit("input_needed", node_id=node.id, name=node.name, question=question)
        ready.wait()
        with self.lock:
            self.pending_inputs.pop(node.id, None)
        return entry["response"]

    def provide_input(self, node_id: str, text: str) -> None:
        entry = self.pending_inputs.get(node_id)
        if not entry:
            raise KeyError("esse agente não está esperando resposta")
        entry["response"] = text
        entry["event"].set()

    def _release_inputs(self) -> None:
        """Solta todos os nós 👤 pendurados (abort): resposta None = abortado."""
        with self.lock:
            entries = list(self.pending_inputs.values())
        for e in entries:
            e["response"] = None
            e["event"].set()

    # -- feedback do usuário para os agentes ----------------------------------
    def add_feedback(self, node_ids: list[str], text: str) -> None:
        keys: set[str] = set()
        for nid in node_ids:
            node = next((n for n in self.graph.nodes if n.id == nid), None)
            keys.add(persona_key(node) if node else nid)
        with self.lock:
            for k in keys:
                self.feedback.setdefault(k, []).append(text)

    def clear_feedback(self, node_id: str | None = None) -> None:
        with self.lock:
            if node_id is None:
                self.feedback.clear()
            else:
                node = next((n for n in self.graph.nodes if n.id == node_id), None)
                self.feedback.pop(persona_key(node) if node else node_id, None)

    def _feedback_for(self, pkey: str) -> list[str]:
        with self.lock:
            return list(self.feedback.get(pkey, []))

    # -- execução -----------------------------------------------------------
    def start_run(self, task: str) -> None:
        if self.phase in BUSY_PHASES:
            raise RuntimeError("já tem uma execução em andamento")
        expanded = expand_loops(self.graph)  # setas 🔁 viram rodadas clonadas
        validate_runnable(expanded)
        self._exec_graph = expanded
        self.task = task
        self.error = ""
        self.pending_approval = ""
        self.report = None
        self.node_status = {n.id: "pending" for n in self.graph.nodes}
        with self.lock:
            # eventos da execução anterior saem da fila (senão um reload ou um
            # cliente novo re-exibe o histórico todo); o seq segue monotônico
            self.events = []
            self.tokens_in = 0
            self.tokens_out = 0
            self.by_model = {}
        self.phase = "running"
        threading.Thread(target=self._run_worker, args=(task,), daemon=True).start()

    def _emit_from_run(self, kind: str, **data) -> None:
        """Executor → UI: espelha o status por nó e registra tokens antes de emitir.

        Ids de rodadas de loop ("n3~2") mapeiam para a caixinha desenhada (n3):
        o nó reacende no canvas a cada rodada.
        """
        nid = data.get("node_id")
        if nid:
            status = {
                "node_start": "running",
                "node_output": "done",
                "node_error": "error",
                "node_skipped": "skipped",
            }.get(kind)
            if status:
                self.node_status[base_id(nid)] = status
        if kind == "node_output":
            exec_graph = self._exec_graph or self.graph
            node = next((n for n in exec_graph.nodes if n.id == nid), None)
            if node and node.model:
                self.record_tokens(node.model, data.get("tokens_in", 0), data.get("tokens_out", 0))
        self.emit(kind, **data)

    def _gate(self, message: str) -> str:
        self.pending_approval = message
        self._decision_ready.clear()
        self.phase = "waiting_approval"
        self.emit("approval_needed", message=message)
        self._decision_ready.wait()
        self.pending_approval = ""
        self.phase = "running"
        return self._decision

    def decide(self, decision: str) -> None:
        if self.phase != "waiting_approval":
            raise RuntimeError("não há aprovação pendente")
        self._decision = decision
        self._decision_ready.set()

    def _run_worker(self, task: str) -> None:
        executor = GraphExecutor(
            self._exec_graph or self.graph,
            self.settings,
            emit=self._emit_from_run,
            text_fn=self.text_fn,
            coder_fn=self.coder_fn,
            gate=self._gate,
            memory=self.memory,
            memory_save=lambda: save_memory(self.memory, self.settings.resolved_project_dir()),
            input_fn=self._ask_input,
            feedback_fn=self._feedback_for,
        )
        self.executor = executor
        try:
            self.report = executor.run(task)
            self.phase = "done"
        except Exception as exc:  # noqa: BLE001 — erro vira estado, não stacktrace na UI
            self.error = f"{type(exc).__name__}: {exc}"
            self.phase = "error"
            self.emit("run_error", error=self.error)
        finally:
            self.executor = None

    def abort(self) -> None:
        ex = self.executor
        if ex:
            ex.request_abort()
        if self.phase == "waiting_approval":
            self.decide("abort")
        self._release_inputs()  # nós 👤 pendurados acordam e são pulados

    def reset(self) -> None:
        if self.phase in BUSY_PHASES:
            raise RuntimeError("aborte a execução antes de resetar")
        self.phase = "idle"
        self.task = ""
        self.error = ""
        self.pending_approval = ""
        self.report = None
        self.node_status = {}
        with self.lock:
            # self.seq NÃO reseta: o cliente guarda lastSeq e filtra por
            # seq > since — se o contador voltasse a 0, os eventos da próxima
            # execução ficariam invisíveis para sempre (feed "travado")
            self.events = []
            self.tokens_in = 0
            self.tokens_out = 0
            self.by_model = {}

    # -- snapshot -----------------------------------------------------------
    def state(self, since: int = 0) -> dict:
        with self.lock:
            actual = sum(cost_for(m, t[0], t[1]) for m, t in self.by_model.items())
            opus = opus_equiv_usd(self.tokens_in, self.tokens_out)
            saved = opus - actual
            return {
                "phase": self.phase,
                "seq": self.seq,
                "task": self.task,
                "error": self.error,
                "pending_approval": self.pending_approval,
                "graph": self.graph.model_dump(mode="json"),
                "node_status": dict(self.node_status),
                "pending_inputs": [
                    {"node_id": e["node_id"], "name": e["name"],
                     "question": e["question"], "context": e["context"]}
                    for e in self.pending_inputs.values()
                ],
                "feedback": {k: list(v) for k, v in self.feedback.items() if v},
                "memory_counts": {k: len(v) for k, v in self.memory.items() if v},
                "tokens_in": self.tokens_in,
                "tokens_out": self.tokens_out,
                "actual_usd": round(actual, 6),
                "saved_usd": round(saved, 6) if actual > 0 and saved > 0 else 0,
                "project_dir": str(self.settings.resolved_project_dir()),
                "events": [e for e in self.events if e["seq"] > since],
            }


class GraphBody(BaseModel):
    graph: dict


class KeyBody(BaseModel):
    name: str
    value: str


class RunBody(BaseModel):
    task: str


class ApproveBody(BaseModel):
    decision: str  # approve | skip | abort


class MemoryClearBody(BaseModel):
    node_id: str | None = None  # None = limpa a memória de todos os agentes


class ProjectDirBody(BaseModel):
    path: str
    create: bool = False  # cria a pasta se não existir


class TraitBody(BaseModel):
    label: str
    prompt: str


class SaveFileBody(BaseModel):
    path: str
    content: str


class RunFileBody(BaseModel):
    path: str


class InstallBody(BaseModel):
    package: str


class InputBody(BaseModel):
    node_id: str
    text: str = ""


class FeedbackBody(BaseModel):
    node_ids: list[str]
    text: str


class FeedbackClearBody(BaseModel):
    node_id: str | None = None  # None = apaga o feedback de todos


class SettingsBody(BaseModel):
    auto_save_code: bool | None = None
    node_timeout_s: int | None = None
    max_attempts: int | None = None


def _resolve_output(report: RunReport | None, node_id: str):
    """Saída de um nó; para nó que rodou em loop, a da ÚLTIMA rodada."""
    if report is None:
        return None
    out = report.outputs.get(node_id)
    if out is not None:
        return out
    best_round, best = -1, None
    prefix = node_id + "~"
    for key, candidate in report.outputs.items():
        if key.startswith(prefix):
            try:
                k = int(key[len(prefix):])
            except ValueError:
                continue
            if k > best_round:
                best_round, best = k, candidate
    return best


def create_app(settings: Settings | None = None, text_fn: TextFn | None = None,
               coder_fn: CoderFn | None = None) -> FastAPI:
    settings = settings or Settings()
    load_api_keys()
    load_custom_traits()
    manager = FlowManager(settings, text_fn=text_fn, coder_fn=coder_fn)

    app = FastAPI(title="ZayderFlow")
    app.state.manager = manager

    @app.get("/")
    def index():
        return FileResponse(WEBAPP_DIR / "index.html")

    @app.get("/logo.png")
    def logo():
        return FileResponse(WEBAPP_DIR / "logo.png")

    @app.get("/favicon.png")
    @app.get("/favicon.ico")
    def favicon():
        return FileResponse(WEBAPP_DIR / "favicon.png")

    @app.get("/api/state")
    def state(since: int = 0):
        return manager.state(since)

    @app.get("/api/graph")
    def get_graph():
        return manager.graph.model_dump(mode="json")

    @app.put("/api/graph")
    def put_graph(body: GraphBody):
        try:
            graph = Graph.model_validate(body.graph)
        except ValidationError as exc:
            raise HTTPException(422, detail=str(exc))
        try:
            manager.set_graph(graph)
        except RuntimeError as exc:
            raise HTTPException(409, detail=str(exc))
        return {"ok": True}

    @app.post("/api/run")
    def run(body: RunBody):
        if not body.task.strip():
            raise HTTPException(422, detail="descreva a tarefa antes de rodar")
        try:
            manager.start_run(body.task.strip())
        except RuntimeError as exc:
            raise HTTPException(409, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc))
        return {"ok": True}

    @app.post("/api/approve")
    def approve(body: ApproveBody):
        if body.decision not in ("approve", "skip", "abort"):
            raise HTTPException(422, detail="decisão inválida")
        try:
            manager.decide(body.decision)
        except RuntimeError as exc:
            raise HTTPException(409, detail=str(exc))
        return {"ok": True}

    @app.post("/api/abort")
    def abort():
        manager.abort()
        return {"ok": True}

    @app.post("/api/reset")
    def reset():
        try:
            manager.reset()
        except RuntimeError as exc:
            raise HTTPException(409, detail=str(exc))
        return {"ok": True}

    @app.get("/api/output/{node_id}")
    def output(node_id: str):
        ex = manager.executor
        report = (ex.report if ex else None) or manager.report
        out = _resolve_output(report, node_id)
        if out is None:
            raise HTTPException(404, detail="esse agente ainda não produziu saída")
        return out.model_dump(mode="json")

    @app.get("/api/templates")
    def templates():
        return list_templates()

    @app.post("/api/templates/{template_id}/load")
    def template_load(template_id: str):
        try:
            graph = load_template(template_id)
        except KeyError:
            raise HTTPException(404, detail="template não encontrado")
        try:
            # o template novo reusa ids (n1, n2…): sem reset, o 📄 do agente novo
            # mostraria a saída do agente ANTIGO da execução anterior
            manager.reset()
            manager.set_graph(graph)
        except RuntimeError as exc:
            raise HTTPException(409, detail=str(exc))
        return graph.model_dump(mode="json")

    @app.post("/api/project-dir")
    def project_dir(body: ProjectDirBody):
        if not body.path.strip():
            raise HTTPException(422, detail="informe um caminho")
        try:
            manager.switch_project(body.path.strip(), create=body.create)
        except RuntimeError as exc:
            raise HTTPException(409, detail=str(exc))
        except FileNotFoundError as exc:
            raise HTTPException(404, detail=str(exc))
        except OSError as exc:
            raise HTTPException(422, detail=f"não consegui usar esse diretório: {exc}")
        return {
            "ok": True,
            "project_dir": str(manager.settings.resolved_project_dir()),
            "graph": manager.graph.model_dump(mode="json"),
        }

    @app.post("/api/input")
    def human_input(body: InputBody):
        try:
            manager.provide_input(body.node_id, body.text)
        except KeyError as exc:
            raise HTTPException(404, detail=str(exc.args[0]))
        return {"ok": True}

    @app.post("/api/feedback")
    def feedback_add(body: FeedbackBody):
        if not body.text.strip():
            raise HTTPException(422, detail="escreva o feedback")
        if not body.node_ids:
            raise HTTPException(422, detail="selecione pelo menos um agente")
        manager.add_feedback(body.node_ids, body.text.strip())
        return {"ok": True, "feedback": {k: list(v) for k, v in manager.feedback.items() if v}}

    @app.post("/api/feedback/clear")
    def feedback_clear(body: FeedbackClearBody):
        manager.clear_feedback(body.node_id)
        return {"ok": True, "feedback": {k: list(v) for k, v in manager.feedback.items() if v}}

    @app.get("/api/settings")
    def settings_get():
        s = manager.settings
        return {
            "auto_save_code": s.auto_save_code,
            "node_timeout_s": s.node_timeout_s,
            "max_attempts": s.max_attempts,
            "project_dir": str(s.resolved_project_dir()),
            "keys": [
                {"name": name, "set": bool(os.environ.get(name)),
                 "tail": (os.environ.get(name) or "")[-4:] if os.environ.get(name) else ""}
                for name in KNOWN_API_KEYS
            ],
        }

    @app.post("/api/settings")
    def settings_set(body: SettingsBody):
        s = manager.settings
        if body.auto_save_code is not None:
            s.auto_save_code = body.auto_save_code
        if body.node_timeout_s is not None:
            s.node_timeout_s = max(10, body.node_timeout_s)
        if body.max_attempts is not None:
            s.max_attempts = max(1, min(5, body.max_attempts))
        try:
            s.save()
        except OSError:
            pass  # projeto somente leitura: vale para a sessão mesmo assim
        return {"ok": True}

    @app.post("/api/memory/clear")
    def memory_clear(body: MemoryClearBody):
        manager.clear_memory(body.node_id)
        return {"ok": True, "memory_counts": {k: len(v) for k, v in manager.memory.items() if v}}

    @app.get("/api/output/{node_id}/blocks")
    def output_blocks(node_id: str):
        ex = manager.executor
        report = (ex.report if ex else None) or manager.report
        out = _resolve_output(report, node_id)
        if out is None:
            raise HTTPException(404, detail="esse agente ainda não produziu saída")
        node = next((n for n in manager.graph.nodes if n.id == node_id), None)
        stem = node.name if node else node_id
        blocks = extract_code_blocks(out.text)
        return [
            {
                "lang": b["lang"],
                "suggested": suggest_filename(b, stem, i),
                "code": b["code"],
                "lines": b["code"].count("\n"),
            }
            for i, b in enumerate(blocks)
        ]

    # -- arquivos do projeto -------------------------------------------------
    @app.get("/api/files")
    def files():
        project = manager.settings.resolved_project_dir()
        out = []
        if project.is_dir():
            for p in sorted(project.rglob("*")):
                rel = p.relative_to(project)
                if any(part in _FILES_SKIP_DIRS for part in rel.parts):
                    continue
                if p.is_file():
                    out.append({"path": str(rel).replace("\\", "/"),
                                "bytes": p.stat().st_size})
                if len(out) >= 500:
                    break
        return {"files": out, "project_dir": str(project)}

    @app.get("/api/file")
    def file_read(path: str):
        project = manager.settings.resolved_project_dir()
        try:
            target = resolve_within(project, path)
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc))
        if not target.is_file():
            raise HTTPException(404, detail="arquivo não encontrado")
        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise HTTPException(500, detail=str(exc))
        truncated = len(content) > FILE_VIEW_MAX_CHARS
        return {
            "path": path,
            "content": content[:FILE_VIEW_MAX_CHARS],
            "truncated": truncated,
            "lines": content.count("\n") + 1,
        }

    @app.post("/api/save-file")
    def save_file(body: SaveFileBody):
        project = manager.settings.resolved_project_dir()
        try:
            target = resolve_within(project, body.path)
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body.content, encoding="utf-8")
        rel = str(target.relative_to(project)).replace("\\", "/")
        return {"ok": True, "path": rel}

    @app.post("/api/run-file")
    def run_file(body: RunFileBody):
        project = manager.settings.resolved_project_dir()
        try:
            target = resolve_within(project, body.path)
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc))
        if not target.is_file():
            raise HTTPException(404, detail="arquivo não encontrado")
        if target.suffix != ".py":
            raise HTTPException(422, detail="só sei rodar arquivos .py")
        env = dict(os.environ, PYTHONPATH=str(project), PYTHONIOENCODING="utf-8")
        try:
            proc = subprocess.run(
                [sys.executable, str(target)],
                cwd=str(project), capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=30, input="", env=env,
            )
            missing = MISSING_MODULE_RE.search(proc.stderr or "")
            module = missing.group(1).split(".")[0] if missing else None
            return {
                "returncode": proc.returncode,
                "timed_out": False,
                "stdout": proc.stdout[:RUN_OUTPUT_MAX_CHARS],
                "stderr": proc.stderr[:RUN_OUTPUT_MAX_CHARS],
                "missing_module": module,
                "suggested_package": MODULE_TO_PACKAGE.get(module, module) if module else None,
            }
        except subprocess.TimeoutExpired as exc:
            return {
                "returncode": None,
                "timed_out": True,
                "stdout": (exc.stdout or "")[:RUN_OUTPUT_MAX_CHARS],
                "stderr": "(interrompido: passou de 30s — programa interativo ou loop infinito?)",
            }

    @app.post("/api/install")
    def install(body: InstallBody):
        pkg = body.package.strip()
        if not PACKAGE_NAME_RE.fullmatch(pkg):
            raise HTTPException(422, detail=f"nome de pacote inválido: {pkg}")
        try:
            proc = _pip_install(pkg)
        except subprocess.TimeoutExpired:
            raise HTTPException(500, detail="pip demorou demais (5 min) — instale manualmente")
        output = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "output": output[-RUN_OUTPUT_MAX_CHARS:],
        }

    @app.post("/api/reveal")
    def reveal():
        project = manager.settings.resolved_project_dir()
        if not project.is_dir():
            raise HTTPException(404, detail="diretório do projeto não existe")
        _open_path(project)
        return {"ok": True}

    @app.get("/api/presets")
    def presets():
        return all_presets()

    @app.get("/api/traits")
    def traits():
        return catalog()

    @app.post("/api/traits")
    def trait_create(body: TraitBody):
        try:
            key = add_custom_trait(body.label, body.prompt)
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc))
        return {"ok": True, "key": key, "traits": catalog()}

    @app.delete("/api/traits/{key}")
    def trait_delete(key: str):
        if not delete_custom_trait(key):
            raise HTTPException(404, detail="só personalidades criadas por você podem ser apagadas")
        return {"ok": True, "traits": catalog()}

    @app.get("/api/models")
    def models():
        return {"models": sorted(PRICES_PER_M.keys())}

    @app.post("/api/keys")
    def keys(body: KeyBody):
        if not body.name.strip() or not body.value.strip():
            raise HTTPException(422, detail="nome e valor são obrigatórios")
        save_api_key(body.name.strip(), body.value.strip())
        return {"ok": True}

    return app
