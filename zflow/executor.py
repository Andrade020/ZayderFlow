"""Interpretador do grafo: executa qualquer DAG de agentes, nível a nível.

É a camada que no Zayder era código imperativo fixo (Planner→Coder→Debugger) e
aqui virou dado: o usuário desenha, este módulo interpreta.

Semântica:
- níveis topológicos (Kahn); nós de TEXTO rodam em paralelo dentro do nível
  (litellm é I/O-bound), nós CODADORES são serializados (dois Aiders no mesmo
  repo corrompem git) e passam pelo gate humano se graph.approve_coder;
- input de um nó = tarefa (se include_task) + saídas dos predecessores que
  deram certo; um nó só é PULADO se TODOS os predecessores falharam/foram
  pulados;
- teto de custo checado entre níveis; abort checado entre níveis e entre nós.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from .codeblocks import save_named_blocks
from .config import Settings
from .models import (
    Graph,
    Node,
    NodeOutput,
    RunReport,
    persona_key,
    topo_levels,
    validate_runnable,
)
from .nodes import build_user_message, run_text_node
from .store import remember

EmitFn = Callable[..., None]
TextFn = Callable[..., NodeOutput]  # (node, user_msg, settings, history=None)
CoderFn = Callable[[Node, str, Settings], NodeOutput]
GateFn = Callable[[str], str]  # retorna "approve" | "skip" | "abort"

PREVIEW_CHARS = 2000


def _preview(text: str) -> tuple[str, bool]:
    if len(text) <= PREVIEW_CHARS:
        return text, False
    return text[:PREVIEW_CHARS], True


def _default_coder_fn(node: Node, user_msg: str, settings: Settings) -> NodeOutput:
    from .coder import run_coder_node

    return run_coder_node(node, user_msg, settings)


class GraphExecutor:
    def __init__(
        self,
        graph: Graph,
        settings: Settings,
        emit: EmitFn = lambda kind, **data: None,
        text_fn: TextFn | None = None,
        coder_fn: CoderFn | None = None,
        gate: GateFn | None = None,
        memory: dict[str, list[dict]] | None = None,
        memory_save: Callable[[], None] | None = None,
    ):
        self.graph = graph
        self.settings = settings
        self.emit = emit
        self.text_fn = text_fn or run_text_node
        self.coder_fn = coder_fn or _default_coder_fn
        self.gate = gate
        self.memory = memory if memory is not None else {}
        self.memory_save = memory_save
        self._abort = threading.Event()
        self._coder_lock = threading.Lock()
        self._report_lock = threading.Lock()  # nós paralelos mutam o report
        self._memory_lock = threading.Lock()
        # contexto DENTRO da execução, por personagem: instâncias duplicadas do
        # mesmo personagem (persona) veem o que as anteriores disseram nesta run
        self._run_history: dict[str, list[dict]] = {}
        self.report: RunReport | None = None  # visível DURANTE a execução (drawer da UI)

    def request_abort(self) -> None:
        self._abort.set()

    # ------------------------------------------------------------------
    def run(self, task: str) -> RunReport:
        validate_runnable(self.graph)
        report = RunReport(task=task)
        self.report = report
        self.emit("run_start", task=task)
        levels = topo_levels(self.graph)

        for level in levels:
            if self._abort.is_set():
                report.aborted = True
                break
            ceiling = self.graph.cost_ceiling_usd
            if ceiling is not None and report.cost_usd_total >= ceiling:
                self.emit(
                    "ceiling",
                    cost_usd=round(report.cost_usd_total, 6),
                    ceiling_usd=ceiling,
                )
                report.aborted = True
                break

            text_nodes = [n for n in level if n.type == "text"]
            coder_nodes = [n for n in level if n.type == "coder"]

            # texto em paralelo (I/O-bound); resultados na ordem de submissão
            if text_nodes:
                with ThreadPoolExecutor(max_workers=self.settings.max_workers) as pool:
                    futures = [
                        pool.submit(self._run_node, n, report) for n in text_nodes
                    ]
                    for f in futures:
                        f.result()
            # codadores em série (e com gate)
            for n in coder_nodes:
                if self._abort.is_set():
                    report.aborted = True
                    break
                self._run_node(n, report)

        if not self._abort.is_set() and not report.aborted:
            report.completed = True
        report.aborted = report.aborted or self._abort.is_set()
        self.emit(
            "run_done",
            completed=report.completed,
            aborted=report.aborted,
            cost_usd_total=round(report.cost_usd_total, 6),
        )
        return report

    # ------------------------------------------------------------------
    def _received_for(self, node: Node, report: RunReport) -> tuple[list[tuple[str, str]], bool]:
        """(mensagens dos predecessores ok, deve_pular?)"""
        preds = self.graph.predecessors(node.id)
        received: list[tuple[str, str]] = []
        for p in preds:
            out = report.outputs.get(p.id)
            if out and not out.error and not out.skipped:
                received.append((p.name, out.text))
        skip = bool(preds) and not received  # todos os predecessores falharam
        return received, skip

    def _skip(self, node: Node, report: RunReport, reason: str) -> None:
        with self._report_lock:
            report.outputs[node.id] = NodeOutput(node_id=node.id, skipped=True, error=reason)
        self.emit("node_skipped", node_id=node.id, name=node.name, reason=reason)

    def _run_node(self, node: Node, report: RunReport) -> None:
        received, skip = self._received_for(node, report)
        if skip:
            self._skip(node, report, "pulado: os agentes anteriores falharam")
            return
        if self._abort.is_set():
            self._skip(node, report, "execução abortada")
            return

        user_msg = build_user_message(report.task, node, received)

        if node.type == "coder" and self.graph.approve_coder and self.gate:
            decision = self.gate(
                f"O agente '{node.name}' vai EDITAR ARQUIVOS do projeto "
                f"({self.settings.resolved_project_dir()}). Aprovar?"
            )
            if decision == "abort":
                self._abort.set()
                self._skip(node, report, "execução abortada")
                return
            if decision == "skip":
                self._skip(node, report, "pulado pelo usuário")
                return

        self.emit("node_start", node_id=node.id, name=node.name, model=node.model)
        pkey = persona_key(node)
        history = None
        if node.type == "text":
            with self._memory_lock:
                persistent = list(self.memory.get(pkey, [])) if node.memory else []
                in_run = list(self._run_history.get(pkey, []))
            history = (persistent + in_run) or None

        max_attempts = max(1, self.settings.max_attempts)
        attempt = 0
        while True:
            attempt += 1
            try:
                if node.type == "coder":
                    with self._coder_lock:
                        out = self.coder_fn(node, user_msg, self.settings)
                else:
                    out = self.text_fn(node, user_msg, self.settings, history=history)
                out.attempts = attempt
                break
            except Exception as exc:  # noqa: BLE001 — erro de nó não derruba o grafo
                if attempt >= max_attempts or self._abort.is_set():
                    out = NodeOutput(node_id=node.id, attempts=attempt,
                                     error=f"{type(exc).__name__}: {exc}")
                    break
                self.emit("node_retry", node_id=node.id, name=node.name,
                          attempt=attempt + 1, max_attempts=max_attempts,
                          error=f"{type(exc).__name__}: {exc}")

        if not out.error and node.type == "text":
            if node.save_files and out.text:
                try:
                    out.files_saved = save_named_blocks(
                        out.text, self.settings.resolved_project_dir())
                except OSError:
                    pass  # disco/permissão: a resposta em si continua válida
            with self._memory_lock:
                self._run_history.setdefault(pkey, []).append(
                    {"user": user_msg, "assistant": out.text})
                if node.memory:
                    remember(self.memory, pkey, user_msg, out.text)
                    if self.memory_save:
                        self.memory_save()

        with self._report_lock:
            report.outputs[node.id] = out
            report.cost_usd_total += out.cost_usd
        if out.error and not out.skipped:
            self.emit("node_error", node_id=node.id, name=node.name, error=out.error,
                      attempts=out.attempts)
        else:
            preview, truncated = _preview(out.text)
            self.emit(
                "node_output",
                node_id=node.id,
                name=node.name,
                preview=preview,
                truncated=truncated,
                tokens_in=out.tokens_in,
                tokens_out=out.tokens_out,
                cost_usd=round(out.cost_usd, 6),
                duration_s=out.duration_s,
                commit_sha=out.commit_sha,
                attempts=out.attempts,
                files_saved=out.files_saved,
            )
