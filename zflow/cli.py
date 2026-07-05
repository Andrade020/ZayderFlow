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

    p_doc = sub.add_parser("doctor", help="checa chaves e ambiente")
    p_doc.set_defaults(func=cmd_doctor)

    args = parser.parse_args(argv)
    if getattr(args, "func", None) is None:
        return cmd_ui(argparse.Namespace(dir=".", port=8430, no_browser=False))
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
