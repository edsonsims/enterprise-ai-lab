"""Run a bounded local privacy resource or its reproducible synthetic experiment."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--issuer", required=True)
    serve.add_argument("--vault", type=Path, required=True)
    serve.add_argument("--processor", help="Operator-configured literal loopback origin")
    serve.add_argument("--ttl-seconds", type=int, default=120)
    serve.add_argument("--port", type=int, default=8091)
    demo = sub.add_parser("demo")
    demo.add_argument("--identity-dir", type=Path, required=True)
    demo.add_argument("--node", default=os.environ.get("NODE_BINARY", "node"))
    demo.add_argument("--work-dir", type=Path, required=True, help="Must not exist")
    demo.add_argument("--out", type=Path, required=True)
    evaluate = sub.add_parser("evaluate")
    evaluate.add_argument("--out", type=Path, required=True)
    check = sub.add_parser("verify-report")
    check.add_argument("report", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "evaluate":
            from .evaluation import evaluate_corpus
            report = evaluate_corpus()
            args.out.parent.mkdir(parents=True, exist_ok=True)
            with args.out.open("x", encoding="utf-8") as stream:
                stream.write(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
            print(json.dumps({"completed": True, "cases": len(report["cases"])}))
            return 0
        if args.command == "verify-report":
            from .evaluation import recompute
            from ..rag.auth import strict_json
            raw = args.report.read_bytes()
            if len(raw) > 2_000_000:
                raise ValueError()
            metrics = recompute(strict_json(raw))
            print(json.dumps({"verified": True, "metrics": metrics}, allow_nan=False))
            return 0
        if args.command == "demo":
            from .integration import run_demo
            report = run_demo(args.identity_dir, args.node, args.work_dir, args.out)
            print(json.dumps({"completed": True, "http_cases": len(report["observations"]),
                              "synthetic_only": True, "restoration_after_restart": True}))
            return 0
        if not 0 <= args.port <= 65535:
            raise ValueError()
        from ..rag.auth import JwtVerifier, PRIVACY_AUDIENCE
        from .api import create_app, LocalProcessor
        from .vault import Vault
        import uvicorn
        verifier = JwtVerifier.discover(args.issuer, audience=PRIVACY_AUDIENCE)
        vault = Vault(args.vault, ttl_seconds=args.ttl_seconds)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            processor = LocalProcessor(args.processor) if args.processor else None
            app = create_app(verifier, vault, processor)
            sock.bind(("127.0.0.1", args.port))
            sock.listen(32)
            print(json.dumps({"event": "ready", "url": f"http://127.0.0.1:{sock.getsockname()[1]}"}), flush=True)
            uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port,
                access_log=False, log_level="error", timeout_keep_alive=2, limit_concurrency=8)).run(sockets=[sock])
        finally:
            sock.close()
            vault.close()
        return 0
    except Exception:
        # Neither exception messages nor dynamic paths/text/credentials are logged.
        print("Privacy command could not complete; check the documented local setup and bounds.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
