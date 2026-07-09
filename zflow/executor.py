"""Interpretador do grafo: executa qualquer DAG de agentes, nível a nível.

É a camada que no Zayder era código imperativo fixo (Planner→Coder→Debugger) e
aqui virou dado: o usuário desenha, este módulo interpreta.

Semântica:
- níveis topológicos (Kahn); nós de TEXTO rodam em paralelo dentro do nível
  (litellm é I/O-bound), nós CODADORES são serializados (dois Aiders no mesmo
  repo corrompem git) e passam pelo gate humano se graph.approve_coder;
- input de um nó = tarefa (se include_task) + saídas dos predecessores que
  deram certo; um nó só é PULADO se TODOS os predecessores falharam/foram
  pulados (ou o ramo foi encerrado por 🛑/condição);
- tipos especiais: human espera resposta do usuário (input_fn), timer espera
  wait_s segundos e repassa as mensagens, stop encerra o ramo, cond roteia
  para UMA das setas de saída (os outros destinos são pulados);
- loops (setas 🔁) NÃO chegam aqui: looping.expand_loops desenrola antes;
- teto de custo checado entre níveis; abort checado entre níveis e entre nós.
"""

from __future__ import annotations

import threading
import time
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
InputFn = Callable[[Node, str, str], "str | None"]  # (node, pergunta, contexto) -> resposta (None = abortou)
FeedbackFn = Callable[[str], list[str]]  # persona_key -> feedbacks pendentes do usuário

PREVIEW_CHARS = 2000

ROUTER_PROMPT = """Você é "{name}", um roteador de fluxo entre agentes de IA.
Sua única função é escolher UMA rota de saída com base no critério abaixo.

Critério de decisão: {criterion}

Rotas possíveis (escolha exatamente uma):
{routes}

Responda com o nome EXATO da rota escolhida na PRIMEIRA linha.
Depois, se quiser, uma justificativa curta (1-2 frases) em português."""


def _parse_route(answer: str, labels: list[str]) -> str:
    """Casa a resposta do modelo com uma rota; em dúvida, fica com a primeira."""
    text = (answer or "").strip()
    first = text.splitlines()[0].strip().strip("\"'`*.:;!") if text else ""
    by_lower = {lab.lower(): lab for lab in labels}
    if first.lower() in by_lower:
        return by_lower[first.lower()]
    for lab in labels:
        if lab.lower() in first.lower():
            return lab
    for lab in labels:
        if lab.lower() in text.lower():
            return lab
    return labels[0]


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
        input_fn: InputFn | None = None,
        feedback_fn: FeedbackFn | None = None,
    ):
        self.graph = graph
        self.settings = settings
        self.emit = emit
        self.text_fn = text_fn or run_text_node
        self.coder_fn = coder_fn or _default_coder_fn
        self.gate = gate
        self.memory = memory if memory is not None else {}
        self.memory_save = memory_save
        self.input_fn = input_fn
        self.feedback_fn = feedback_fn
        # destinos NÃO escolhidos por nós de condição: (cond_id, target_id)
        self._cond_skip: set[tuple[str, str]] = set()
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

            # tudo que não edita arquivos roda em paralelo (texto, condição,
            # humano, timer, parada — I/O-bound ou instantâneo)
            text_nodes = [n for n in level if n.type != "coder"]
            coder_nodes = [n for n in level if n.type == "coder"]

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
    def _received_for(
        self, node: Node, report: RunReport
    ) -> tuple[list[tuple[str, str]], bool, str]:
        """(mensagens dos predecessores ok, deve_pular?, motivo do pulo)"""
        preds = self.graph.predecessors(node.id)
        received: list[tuple[str, str]] = []
        saw_stop = saw_cond = False
        for p in preds:
            out = report.outputs.get(p.id)
            ok = bool(out) and not out.error and not out.skipped
            if ok and (p.id, node.id) in self._cond_skip:
                ok = False  # a condição rodou, mas escolheu outra rota
                saw_cond = True
            if ok:
                received.append((p.name, out.text))
            elif p.type == "stop":
                saw_stop = True
        skip = bool(preds) and not received  # nenhum predecessor entregou nada
        if not skip:
            return received, False, ""
        if saw_cond:
            reason = "pulado: a condição escolheu outra rota"
        elif saw_stop:
            reason = "pulado: o ramo foi encerrado por uma 🛑 parada"
        else:
            reason = "pulado: os agentes anteriores falharam"
        return received, True, reason

    def _skip(self, node: Node, report: RunReport, reason: str) -> None:
        with self._report_lock:
            report.outputs[node.id] = NodeOutput(node_id=node.id, skipped=True, error=reason)
        self.emit("node_skipped", node_id=node.id, name=node.name, reason=reason)

    def _routes_for(self, node: Node) -> list[tuple[str, list[str]]]:
        """Rotas de um nó de condição: [(rótulo, [ids de destino])], na ordem
        das setas. Rótulo vazio vira o nome do nó de destino."""
        routes: list[tuple[str, list[str]]] = []
        for e in self.graph.edges:
            if e.source != node.id:
                continue
            label = e.label.strip() or self.graph.node(e.target).name
            for lab, targets in routes:
                if lab.lower() == label.lower():
                    targets.append(e.target)
                    break
            else:
                routes.append((label, [e.target]))
        return routes

    def _finish(self, node: Node, report: RunReport, out: NodeOutput, **extra) -> None:
        """Registra o resultado de um nó e emite node_output/node_error."""
        with self._report_lock:
            report.outputs[node.id] = out
            report.cost_usd_total += out.cost_usd
        if out.error and not out.skipped:
            self.emit("node_error", node_id=node.id, name=node.name, error=out.error,
                      attempts=out.attempts)
            return
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
            **extra,
        )

    def _run_node(self, node: Node, report: RunReport) -> None:
        received, skip, skip_reason = self._received_for(node, report)
        if skip:
            self._skip(node, report, skip_reason)
            return
        if self._abort.is_set():
            self._skip(node, report, "execução abortada")
            return

        # ---- tipos especiais que não chamam modelo -----------------------
        if node.type == "stop":
            # encerra ESTE ramo: quem depende só dele será pulado; o resto segue
            self._skip(node, report, "🛑 parada — este ramo termina aqui")
            return

        if node.type == "timer":
            self.emit("node_start", node_id=node.id, name=node.name, model="",
                      wait_s=node.wait_s)
            started = time.monotonic()
            if self._abort.wait(max(0.0, node.wait_s)):
                self._skip(node, report, "execução abortada")
                return
            out = NodeOutput(node_id=node.id,
                             text="\n\n".join(text for _, text in received),
                             duration_s=round(time.monotonic() - started, 2))
            self._finish(node, report, out)
            return

        user_msg = build_user_message(report.task, node, received)
        fb = self.feedback_fn(persona_key(node)) if self.feedback_fn else []
        if fb:
            user_msg += ("\n\n## Feedback do supervisor humano (siga estas orientações)\n"
                         + "\n".join(f"- {f}" for f in fb))

        if node.type == "human":
            self.emit("node_start", node_id=node.id, name=node.name, model="")
            question = node.extra_prompt.strip() or "Escreva sua contribuição para o fluxo:"
            started = time.monotonic()
            answer = self.input_fn(node, question, user_msg) if self.input_fn else ""
            if answer is None or self._abort.is_set():
                self._skip(node, report, "execução abortada")
                return
            out = NodeOutput(node_id=node.id, text=answer.strip(),
                             duration_s=round(time.monotonic() - started, 2))
            pkey = persona_key(node)
            with self._memory_lock:
                self._run_history.setdefault(pkey, []).append(
                    {"user": user_msg, "assistant": out.text})
            self._finish(node, report, out)
            return

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
        if node.type in ("text", "cond"):
            with self._memory_lock:
                persistent = list(self.memory.get(pkey, [])) if node.memory else []
                in_run = list(self._run_history.get(pkey, []))
            history = (persistent + in_run) or None

        # nó de condição vira uma chamada de texto com prompt de roteador:
        # as setas de saída são as rotas, o extra_prompt é o critério
        call_node = node
        routes: list[tuple[str, list[str]]] = []
        if node.type == "cond":
            routes = self._routes_for(node)
            if routes:
                call_node = node.model_copy(update={"prompt_override": ROUTER_PROMPT.format(
                    name=node.name,
                    criterion=node.extra_prompt.strip() or "escolha a rota mais adequada às mensagens recebidas",
                    routes="\n".join(f"- {lab}" for lab, _ in routes),
                )})

        max_attempts = max(1, self.settings.max_attempts)
        attempt = 0
        while True:
            attempt += 1
            try:
                if node.type == "coder":
                    with self._coder_lock:
                        out = self.coder_fn(node, user_msg, self.settings)
                else:
                    out = self.text_fn(call_node, user_msg, self.settings, history=history)
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

        chosen = None
        if not out.error and node.type == "cond" and len(routes) > 1:
            chosen = _parse_route(out.text, [lab for lab, _ in routes])
            chosen_targets = dict(routes)[chosen]
            for lab, targets in routes:
                for t in targets:
                    if t not in chosen_targets:
                        self._cond_skip.add((node.id, t))

        if not out.error and node.type in ("text", "cond"):
            if (node.save_files or self.settings.auto_save_code) \
                    and node.type == "text" and out.text:
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

        out.node_id = node.id  # cond usa uma cópia do nó; garante o id certo
        self._finish(node, report, out, **({"chosen": chosen} if chosen else {}))
