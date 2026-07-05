"""CLI do ZayderFlow. `zflow` sem subcomando abre a UI (como no Zayder)."""

from __future__ import annotations

import argparse
import socket
import threading
import webbrowser

from .config import load_api_keys, load_settings


def _pick_port(preferred: int) -> int:
    """Tenta a porta preferida; ocupada, deixa o SO escolher uma livre.

    Bind exclusivo de propósito (sem SO_REUSEADDR): no Windows, REUSEADDR
    mascararia "porta em uso" e dois zflow brigariam pela mesma porta.
    """
    for port in (preferred, 0):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", port))
            chosen = s.getsockname()[1]
            s.close()
            return chosen
        except OSError:
            s.close()
    return preferred


def cmd_ui(args: argparse.Namespace) -> int:
    import uvicorn

    from .server import create_app

    settings = load_settings(args.dir)
    app = create_app(settings)
    port = _pick_port(args.port)
    if port != args.port:
        print(f"porta {args.port} ocupada; usando {port}")
    url = f"http://127.0.0.1:{port}"
    print(f"ZayderFlow em {url}  (Ctrl+C para sair)")
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Roda um grafo sem UI (bom para scripts e testes de fumaça)."""
    import json
    from pathlib import Path

    from .executor import GraphExecutor
    from .models import Graph
    from .store import load_graph

    load_api_keys()
    settings = load_settings(args.dir)
    if args.graph:
        data = json.loads(Path(args.graph).read_text(encoding="utf-8-sig"))
        data.pop("template_id", None)
        data.pop("description", None)
        graph = Graph.model_validate(data)
    else:
        graph = load_graph(settings.resolved_project_dir())

    def emit(kind: str, **d) -> None:
        if kind == "node_start":
            print(f"  … {d['name']} pensando ({d['model']})")
        elif kind == "node_output":
            print(f"  ok {d['name']}  ({d['tokens_out']} tokens, ${d['cost_usd']:.4f})")
        elif kind == "node_error":
            print(f"  ERRO {d['name']}: {d['error']}")
        elif kind == "node_skipped":
            print(f"  -- {d['name']}: {d.get('reason', 'pulado')}")
        elif kind == "ceiling":
            print(f"  teto de custo atingido (${d['cost_usd']:.4f})")

    def gate(message: str) -> str:
        if args.yes:
            return "approve"
        ans = input(f"{message} [s = aprovar / p = pular / a = abortar] ").strip().lower()
        return {"s": "approve", "p": "skip", "a": "abort"}.get(ans, "skip")

    executor = GraphExecutor(graph, settings, emit=emit, gate=gate)
    try:
        report = executor.run(args.task)
    except ValueError as exc:
        print(f"erro: {exc}")
        return 2
    print(f"\ncusto total: ${report.cost_usd_total:.4f}")
    for nid, out in report.outputs.items():
        node = graph.node(nid)
        header = f"--- {node.name} ---"
        print(f"\n{header}\n{out.text or out.error}")
    return 0 if report.completed else 1


def cmd_doctor(args: argparse.Namespace) -> int:
    applied = load_api_keys()
    import os

    known = [k for k in os.environ if k.endswith("_API_KEY")]
    print(f"chaves no ambiente: {', '.join(sorted(known)) or 'nenhuma'}")
    if applied:
        print(f"aplicadas de keys.json: {', '.join(sorted(applied))}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="zflow", description="ZayderFlow: fluxos multi-agente")
    sub = parser.add_subparsers(dest="command")

    p_ui = sub.add_parser("ui", help="abre a interface web")
    p_ui.add_argument("--dir", default=".", help="diretório do projeto")
    p_ui.add_argument("--port", type=int, default=8430)
    p_ui.add_argument("--no-browser", action="store_true")
    p_ui.set_defaults(func=cmd_ui)

    p_run = sub.add_parser("run", help="roda um grafo sem abrir a UI")
    p_run.add_argument("--task", required=True, help="a tarefa a executar")
    p_run.add_argument("--graph", default=None, help="arquivo JSON do grafo (default: .zflow/graph.json)")
    p_run.add_argument("--dir", default=".", help="diretório do projeto")
    p_run.add_argument("--yes", action="store_true", help="aprova nós codadores sem perguntar")
    p_run.set_defaults(func=cmd_run)

    p_doc = sub.add_parser("doctor", help="checa chaves e ambiente")
    p_doc.set_defaults(func=cmd_doctor)

    args = parser.parse_args(argv)
    if getattr(args, "func", None) is None:
        return cmd_ui(argparse.Namespace(dir=".", port=8430, no_browser=False))
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
