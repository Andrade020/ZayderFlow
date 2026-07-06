"""Servidor local do ZayderFlow (FastAPI + polling, sem websockets).

Mesmo padrão do Zayder: um FlowManager com `seq` monotônico + lock protege o
estado; o frontend faz GET /api/state?since=N a cada segundo e recebe só os
eventos novos. Localhost, um usuário — polling é suficiente e simples.

Eventos carregam só um preview (~2KB) da saída de cada nó; o texto completo sai
por GET /api/output/{node_id} — cinco analistas × 8KB a cada tick estourariam o
payload do polling.
"""

from __future__ import annotations

import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ValidationError

from .config import Settings, load_api_keys, save_api_key
from .executor import CoderFn, GraphExecutor, TextFn
from .gallery import list_templates, load_template
from .models import Graph, RunReport, validate_runnable
from .pricing import PRICES_PER_M, cost_for, opus_equiv_usd
from .store import load_graph, save_graph
from .traits import TRAIT_LABELS, TRAITS

WEBAPP_DIR = Path(__file__).parent / "webapp"

BUSY_PHASES = ("running", "waiting_approval")


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

    # -- execução -----------------------------------------------------------
    def start_run(self, task: str) -> None:
        if self.phase in BUSY_PHASES:
            raise RuntimeError("já tem uma execução em andamento")
        validate_runnable(self.graph)
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
        """Executor → UI: espelha o status por nó e registra tokens antes de emitir."""
        nid = data.get("node_id")
        if nid:
            status = {
                "node_start": "running",
                "node_output": "done",
                "node_error": "error",
                "node_skipped": "skipped",
            }.get(kind)
            if status:
                self.node_status[nid] = status
        if kind == "node_output":
            node = next((n for n in self.graph.nodes if n.id == nid), None)
            if node:
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
            self.graph,
            self.settings,
            emit=self._emit_from_run,
            text_fn=self.text_fn,
            coder_fn=self.coder_fn,
            gate=self._gate,
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


def create_app(settings: Settings | None = None, text_fn: TextFn | None = None,
               coder_fn: CoderFn | None = None) -> FastAPI:
    settings = settings or Settings()
    load_api_keys()
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
        out = report.outputs.get(node_id) if report else None
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
            manager.set_graph(graph)
        except RuntimeError as exc:
            raise HTTPException(409, detail=str(exc))
        return graph.model_dump(mode="json")

    @app.get("/api/traits")
    def traits():
        return [
            {"key": k, "label": TRAIT_LABELS.get(k, k), "prompt": v}
            for k, v in TRAITS.items()
        ]

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
