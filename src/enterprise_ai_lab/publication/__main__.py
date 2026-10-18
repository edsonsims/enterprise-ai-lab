"""Trusted local staging, authenticated approval and single-owner RAG serving."""
from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import json
from pathlib import Path
import socket
import sys


class PublishedPipeline:
    """Hold the vector lock through both the publication fence and retrieval."""
    def __init__(self, store, publisher):
        from ..rag.pipeline import RagPipeline
        self.store, self.publisher = store, publisher
        self.pipeline = RagPipeline(store)

    def query(self, *args, **kwargs):
        from .activities import PublicationBlocked
        # Do not queue an already authenticated request behind a potentially
        # long publication and then retrieve using an expired short-lived JWT.
        if not self.store.lock.acquire(blocking=False):
            raise PublicationBlocked("publication busy; retry explicitly")
        try:
            self.publisher.assert_readable()
            return self.pipeline.query(*args, **kwargs)
        finally:
            self.store.lock.release()


async def run(args):
    if args.command == "demo":
        from .integration import run_demo
        result = await run_demo(temporal_cli=Path(args.temporal_cli), identity_dir=Path(args.identity_dir),
            node=args.node, model_dir=Path(args.model_dir), manifest_path=Path(args.manifest),
            work_dir=Path(args.work_dir), out=Path(args.out))
        print(json.dumps({"output": args.out, "completed": True,
            "http_observations": len(result["observations"]), "publications": len(result["effects"])}))
        return
    from .storage import CandidateRepository
    candidates = CandidateRepository(Path(args.database))
    if args.command == "stage":
        from ..rag.pdf_corpus import load_pdf_documents
        documents = load_pdf_documents(Path(args.manifest))
        groups = {}
        for document in documents:
            groups.setdefault(document.tenant_id, []).append(document)
        metadata = [asdict(candidates.stage(batch)) for batch in groups.values()]
        print(json.dumps({"event": "staged", "candidates": metadata}, ensure_ascii=False, allow_nan=False))
        return
    from ..approval.__main__ import connect
    from ..approval.contracts import identifier
    identifier(args.task_queue)
    client = await connect(args.address, "publication-local-resource" if args.command == "resource" else "publication-local-edge")
    from ..rag.auth import JwtVerifier, APPROVAL_AUDIENCE
    import uvicorn
    if not 0 <= args.port <= 65535:
        raise ValueError("invalid local HTTP port")
    store = None
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", args.port)); sock.listen(128)
        url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        if args.command == "serve":
            from .api import create_app
            from .gateway import PublicationGateway
            verifier = JwtVerifier.discover(args.issuer, audience=APPROVAL_AUDIENCE)
            app = create_app(verifier, PublicationGateway(client, args.task_queue, candidates))
            print(json.dumps({"event": "ready", "url": url}), flush=True)
            await uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="warning",
                timeout_keep_alive=2, limit_concurrency=32)).serve(sockets=[sock])
            return
        from ..rag.api import create_app
        from ..rag.models import LocalEmbedder
        from ..rag.store import VectorStore
        from temporalio.worker import Worker
        from .activities import DocumentPublisher
        from .workflow import DocumentPublicationWorkflow
        verifier = JwtVerifier.discover(args.issuer)
        store = VectorStore(Path(args.store), LocalEmbedder(Path(args.model_dir)))
        publisher = DocumentPublisher(candidates, store, pause_after_commit_seconds=args.pause_after_commit)
        app = create_app(verifier, PublishedPipeline(store, publisher))
        with ThreadPoolExecutor(max_workers=4) as executor:
            async with Worker(client, task_queue=args.task_queue, workflows=[DocumentPublicationWorkflow],
                activities=[publisher.publish_approved_documents], activity_executor=executor):
                print(json.dumps({"event": "ready", "url": url}), flush=True)
                await uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="warning",
                    timeout_keep_alive=2, limit_concurrency=16)).serve(sockets=[sock])
    finally:
        sock.close()
        if store is not None:
            store.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    stage = commands.add_parser("stage", help="extract trusted PDF grants into immutable staging; does not index")
    stage.add_argument("--manifest", required=True)
    stage.add_argument("--database", required=True)
    serve = commands.add_parser("serve", help="JWT-protected document-publication approval API")
    resource = commands.add_parser("resource", help="single-owner query resource and approved-publication worker")
    for command in (serve, resource):
        command.add_argument("--address", required=True)
        command.add_argument("--task-queue", default="publication-v1")
        command.add_argument("--issuer", required=True)
        command.add_argument("--database", required=True)
        command.add_argument("--port", type=int, default=0)
    resource.add_argument("--model-dir", required=True)
    resource.add_argument("--store", required=True)
    resource.add_argument("--pause-after-commit", type=float, default=0,
        help="trusted crash-study hook, 0–15 seconds")
    demo = commands.add_parser("demo", help="actual public PDF/OCR, approval, vector search, crash and replay")
    for name in ("temporal-cli", "identity-dir", "model-dir", "manifest"):
        demo.add_argument("--" + name, required=True)
    demo.add_argument("--node", default="node")
    demo.add_argument("--work-dir", default=".local/publication-demo")
    demo.add_argument("--out", default="reports/local/publication-demo.json")
    args = parser.parse_args(argv)
    try:
        asyncio.run(run(args))
        return 0
    except (ImportError, ValueError, OSError, RuntimeError, TimeoutError) as exc:
        print(f"Publication command failed ({type(exc).__name__}); inspect the documented local setup.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
