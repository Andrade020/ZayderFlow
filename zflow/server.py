"""Servidor local do ZayderFlow (FastAPI + polling, sem websockets).

Mesmo padrão do Zayder: um FlowManager com `seq` monotônico + lock protege o
estado; o frontend faz GET /api/state?since=N a cada segundo e recebe só os
eventos novos. Localhost, um usuário — polling é suficiente e simples.
"""

from __future__ import annotations

import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ValidationError

from .config import Settings, load_api_keys, save_api_key
from .models import Graph
from .pricing import PRICES_PER_M, cost_for, opus_equiv_usd
from .store import load_graph, save_graph
from .traits import TRAIT_LABELS, TRAITS

WEBAPP_DIR = Path(__file__).parent / "webapp"


class FlowManager:
    """Estado central em memória. Fases: idle → running ⇄ waiting_approval → done | error."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.lock = threading.Lock()
        self.phase = "idle"
        self.seq = 0
        self.events: list[dict] = []
        self.graph: Graph = load_graph(settings.resolved_project_dir())
        self.node_status: dict[str, str] = {}  # id -> pending|running|done|error|skipped
        self.outputs: dict[str, dict] = {}  # id -> NodeOutput dump (texto completo)
        self.task = ""
        self.error = ""
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
        if self.phase == "running" or self.phase == "waiting_approval":
            raise RuntimeError("não dá para editar o grafo com uma execução em andamento")
        self.graph = graph
        save_graph(graph, self.settings.resolved_project_dir())

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


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    load_api_keys()
    manager = FlowManager(settings)

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
