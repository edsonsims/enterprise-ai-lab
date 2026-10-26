"""Serve the bounded local admin portal or run its offline process experiment."""

import argparse
import asyncio
import json
import socket
from pathlib import Path


async def serve(args):
    import uvicorn

    from ..mcp_gateway.store import ReadOnlyStore
    from .api import create_app
    from .auth import AdminTokenVerifier
    from .core import AdminStore

    if not 0 <= args.port <= 65535:
        raise ValueError("invalid port")
    verifier = AdminTokenVerifier.discover(args.issuer)
    source = ReadOnlyStore(args.source)
    service = sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        service = AdminStore(args.database, source)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", args.port))
        sock.listen(32)
        origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
        app = create_app(origin=origin, verifier=verifier, service=service)
        print(json.dumps({"event": "ready", "url": origin}), flush=True)
        await uvicorn.Server(
            uvicorn.Config(
                app,
                access_log=False,
                log_level="critical",
                timeout_keep_alive=2,
                limit_concurrency=32,
            )
        ).serve(sockets=[sock])
    finally:
        if sock is not None:
            sock.close()
        if service is not None:
            service.close()
        source.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    server = commands.add_parser(
        "serve", help="dedicated account login and fictional configuration only"
    )
    server.add_argument("--issuer", required=True)
    server.add_argument("--source", type=Path, required=True)
    server.add_argument("--database", type=Path, required=True)
    server.add_argument("--port", type=int, default=8091)
    demo = commands.add_parser(
        "demo", help="real OAuth/HTTP/SQLite/restart/fault experiment"
    )
    demo.add_argument("--identity-dir", type=Path, required=True)
    demo.add_argument("--node", default="node")
    demo.add_argument("--work-dir", type=Path, default=Path(".local/admin-demo"))
    demo.add_argument("--out", type=Path, default=Path("reports/local/admin-demo.json"))
    demo.add_argument("--browser-test", action="store_true")
    demo.add_argument(
        "--browser-script",
        type=Path,
        help="explicit source-tree browser.mjs; required with --browser-test",
    )
    check = commands.add_parser(
        "verify-report", help="offline current-source and recorded-receipt verification"
    )
    check.add_argument("--report", type=Path, required=True)
    check.add_argument("--identity-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            asyncio.run(serve(args))
        elif args.command == "demo":
            from .integration import run_demo

            run_demo(args)
        else:
            from .integration import load_report, verify_report

            result = verify_report(
                load_report(args.report), identity_dir=args.identity_dir
            )
            print(json.dumps(result, sort_keys=True))
        return 0
    except Exception:
        # The trusted host can inspect private state; no exception/path/credential
        # is reflected into public CLI diagnostics.
        print(json.dumps({"ok": False, "error": "admin_failed"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
