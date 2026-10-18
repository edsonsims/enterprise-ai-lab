"""Run a trusted local Temporal worker, authenticated edge, or recorded demo."""
from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import socket
import sys


def local_address(value: str) -> str:
    match = re.fullmatch(r"127\.0\.0\.1:([1-9][0-9]{0,4})", value)
    if not match or not 1 <= int(match[1]) <= 65535:
        raise ValueError("Temporal must use explicit literal loopback and port")
    return value


async def connect(address, identity="approval-local-edge"):
    from temporalio.client import Client
    return await asyncio.wait_for(Client.connect(local_address(address), namespace="default", identity=identity), timeout=5)


async def run(args):
    if args.command == "demo":
        from .integration import run_demo
        result = await run_demo(Path(args.temporal_cli), Path(args.identity_dir), args.node,
            Path(args.work_dir), Path(args.out))
        print(json.dumps({"output": args.out, "completed": True, "observations": len(result["observations"])}))
        return
    client = await connect(args.address, "approval-local-worker" if args.command == "worker" else "approval-local-edge")
    if args.command == "worker":
        from temporalio.worker import Worker
        from .activities import LocalNotifier
        from .contracts import identifier
        from .workflow import ApprovalWorkflow
        identifier(args.task_queue)
        notifier = LocalNotifier(Path(args.notifier_db), pause_after_commit_seconds=args.pause_after_commit,
            fault_once_after_commit=args.fault_after_commit)
        with ThreadPoolExecutor(max_workers=4) as executor:
            async with Worker(client, task_queue=args.task_queue, workflows=[ApprovalWorkflow],
                activities=[notifier.publish_approved_note], activity_executor=executor):
                print(json.dumps({"event": "ready", "worker": "approval"}), flush=True)
                await asyncio.Event().wait()
        return
    from ..rag.auth import APPROVAL_AUDIENCE, JwtVerifier
    from .api import create_app
    from .gateway import ApprovalGateway
    import uvicorn
    verifier = JwtVerifier.discover(args.issuer, audience=APPROVAL_AUDIENCE)
    if not 0 <= args.port <= 65535:
        raise ValueError("invalid local HTTP port")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", args.port)); sock.listen(128)
        app = create_app(verifier, ApprovalGateway(client, args.task_queue))
        print(json.dumps({"event": "ready", "url": f"http://127.0.0.1:{sock.getsockname()[1]}"}), flush=True)
        await uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="warning",
            timeout_keep_alive=2, limit_concurrency=32)).serve(sockets=[sock])
    finally:
        sock.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    worker = commands.add_parser("worker", help="trusted local Temporal worker and SQLite notification sink")
    serve = commands.add_parser("serve", help="JWT-protected local approval HTTP API")
    for command in (worker, serve):
        command.add_argument("--address", required=True)
        command.add_argument("--task-queue", default="approval-v1")
    worker.add_argument("--notifier-db", required=True)
    worker.add_argument("--pause-after-commit", type=float, default=0, help="trusted crash-study fault hook, 0–15 seconds")
    worker.add_argument("--fault-after-commit", action="store_true", help="trusted lost-acknowledgement study hook")
    serve.add_argument("--issuer", required=True)
    serve.add_argument("--port", type=int, default=8089)
    demo = commands.add_parser("demo", help="real persistent Temporal, OAuth/HTTP, crash and replay demonstration")
    demo.add_argument("--temporal-cli", required=True)
    demo.add_argument("--identity-dir", required=True)
    demo.add_argument("--node", default="node")
    demo.add_argument("--work-dir", default=".local/approval-demo")
    demo.add_argument("--out", default="reports/local/approval-demo.json")
    args = parser.parse_args(argv)
    try:
        asyncio.run(run(args))
        return 0
    except (ImportError, ValueError, OSError, RuntimeError, TimeoutError) as exc:
        print(f"Approval command failed ({type(exc).__name__}); inspect the documented local setup.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
