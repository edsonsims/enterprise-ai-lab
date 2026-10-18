"""Explicit local setup, serving and cross-repository RAG demonstration."""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    serve = commands.add_parser('serve', help='serve a local JWT-protected vector RAG resource')
    serve.add_argument('--issuer', required=True)
    serve.add_argument('--model-dir', required=True)
    serve.add_argument('--store', required=True)
    serve.add_argument('--port', type=int, default=8088)
    serve.add_argument('--seed-fixture')
    serve.add_argument('--generator', choices=('extractive', 'qwen'), default='extractive')
    serve.add_argument('--generator-model-dir')
    demo = commands.add_parser('demo', help='exercise actual identity, HTTP, embeddings and persisted vectors')
    demo.add_argument('--identity-dir', required=True)
    demo.add_argument('--model-dir', required=True)
    demo.add_argument('--store', required=True)
    demo.add_argument('--out', default='reports/local/rag-demo.json')
    demo.add_argument('--node', default=os.environ.get('NODE_BINARY', 'node'))
    demo.add_argument('--generator', choices=('extractive', 'qwen'), default='extractive')
    demo.add_argument('--generator-model-dir')
    ingest = commands.add_parser('ingest', help='index trusted local PDF grants, including actual OCR')
    pdf_demo = commands.add_parser('pdf-demo', help='exercise public PDF/OCR citations over actual OAuth and HTTP')
    for command in (ingest, pdf_demo):
        command.add_argument('--manifest', required=True)
        command.add_argument('--model-dir', required=True)
        command.add_argument('--store', required=True)
        command.add_argument('--out', default='reports/local/pdf-demo.json' if command is pdf_demo else 'reports/local/pdf-ingestion.json')
    pdf_demo.add_argument('--identity-dir', required=True)
    pdf_demo.add_argument('--node', default=os.environ.get('NODE_BINARY', 'node'))
    args = parser.parse_args(argv)
    try:
        if args.command == 'pdf-demo':
            from .pdf_demo import run_pdf_demo
            result = run_pdf_demo(manifest_path=Path(args.manifest), identity_dir=Path(args.identity_dir),
                model_dir=Path(args.model_dir), store_path=Path(args.store), out=Path(args.out), node=args.node)
            print(json.dumps({'output': args.out, 'completed': True, 'cases': len(result['cases']),
                'page_grants': result['ingestion']['page_grants']}))
            return 0
        if args.command == 'ingest':
            from .models import LocalEmbedder
            from .store import VectorStore
            from .pdf_corpus import ingest_pdf_manifest
            store = VectorStore(Path(args.store), LocalEmbedder(Path(args.model_dir)))
            try:
                result = ingest_pdf_manifest(Path(args.manifest), store)
            finally:
                store.close()
            output = Path(args.out)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False) + '\n')
            print(json.dumps({'output': args.out, 'page_grants': result['page_grants'], 'chunks_indexed': result['chunks_indexed']}))
            return 0
        if args.command == 'demo':
            from .integration import run_demo
            result = run_demo(identity_dir=Path(args.identity_dir), model_dir=Path(args.model_dir),
                store_path=Path(args.store), out=Path(args.out), generator=args.generator,
                generator_model_dir=Path(args.generator_model_dir) if args.generator_model_dir else None,
                node=args.node)
            summary = {'output':args.out, 'completed':True, 'generator':args.generator}
            if 'neural_observations' in result:
                summary['neural_observations'] = {key: result['neural_observations'][key]
                    for key in ('accepted_responses', 'generation_errors', 'errors_preserved', 'no_fallback')}
            print(json.dumps(summary, allow_nan=False))
            return 0
        if not 0 <= args.port <= 65535:
            raise ValueError('The port must be between 0 and 65535.')
        from .api import create_app
        from .auth import JwtVerifier
        from .models import LocalEmbedder
        from .store import VectorStore
        from .fixtures import load_documents
        from .pipeline import RagPipeline
        import uvicorn
        verifier = JwtVerifier.discover(args.issuer)
        embedder = LocalEmbedder(Path(args.model_dir))
        generator = None
        if args.generator == 'qwen':
            if not args.generator_model_dir:
                raise ValueError('Qwen requires an explicit verified local --generator-model-dir.')
            from .neural import LocalQwenGenerator
            generator = LocalQwenGenerator(Path(args.generator_model_dir))
        store = VectorStore(Path(args.store), embedder)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if args.seed_fixture:
                store.index(load_documents(Path(args.seed_fixture)))
            app = create_app(verifier, RagPipeline(store, generator=generator))
            sock.bind(('127.0.0.1', args.port))
            sock.listen(128)
            url = f'http://127.0.0.1:{sock.getsockname()[1]}'
            print(json.dumps({'event':'ready','url':url}), flush=True)
            uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=args.port,
                access_log=False, log_level='warning', timeout_keep_alive=2,
                limit_concurrency=16)).run(sockets=[sock])
        finally:
            sock.close()
            store.close()
        return 0
    except (ImportError, OSError, ValueError, RuntimeError) as exc:
        # Configuration failures are intentionally generic: no token, body or secret log.
        print(f'RAG command failed ({type(exc).__name__}); inspect local setup and documented bounds.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
