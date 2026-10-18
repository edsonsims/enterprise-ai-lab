"""Serve a real local browser portal or run its actual integration demo."""
import argparse
import asyncio
import json
from pathlib import Path
import socket
import sys


async def serve(args):
    from ..approval.__main__ import connect
    from ..publication.gateway import PublicationGateway
    from ..publication.storage import CandidateRepository
    from .assets import PdfAssets
    from .auth import HumanTokenVerifier
    from .core import HumanReviewService
    from .api import create_app
    import uvicorn
    client = await connect(args.address, "human-review-local-portal")
    verifier = HumanTokenVerifier.discover(args.issuer)
    candidates = CandidateRepository(Path(args.database))
    service = HumanReviewService(candidates, PublicationGateway(client, args.task_queue, candidates), Path(args.audit_database))
    if not 0 <= args.port <= 65535:
        raise ValueError("invalid portal port")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", args.port)); sock.listen(128)
        origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
        app = create_app(origin=origin, verifier=verifier, service=service, assets=PdfAssets(Path(args.manifest)))
        print(json.dumps({"event": "ready", "url": origin}), flush=True)
        await uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="warning",
            timeout_keep_alive=2, limit_concurrency=32)).serve(sockets=[sock])
    finally:
        sock.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    server = commands.add_parser("serve", help="OIDC login, permitted page previews, review and audit")
    for name in ("address", "issuer", "database", "audit-database", "manifest"):
        server.add_argument("--" + name, required=True)
    server.add_argument("--task-queue", default="publication-v1")
    server.add_argument("--port", type=int, default=8090)
    demo = commands.add_parser("demo", help="real local login, PDF preview, decisions, audit and RAG")
    for name in ("temporal-cli", "identity-dir", "model-dir", "manifest"):
        demo.add_argument("--" + name, required=True)
    demo.add_argument("--node", default="node")
    demo.add_argument("--work-dir", default=".local/review-demo")
    demo.add_argument("--out", default="reports/local/review-demo.json")
    demo.add_argument("--stay-open", action="store_true", help="keep local fixture services for browser review; private credentials in work directory")
    demo.add_argument("--browser-test", action="store_true", help="run the optional actual Chromium interface checks")
    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            asyncio.run(serve(args))
        else:
            from .integration import run_demo
            asyncio.run(run_demo(args))
        return 0
    except (ImportError, ValueError, OSError, RuntimeError, TimeoutError) as exc:
        print(f"Review command failed ({type(exc).__name__}); inspect the documented local setup.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
