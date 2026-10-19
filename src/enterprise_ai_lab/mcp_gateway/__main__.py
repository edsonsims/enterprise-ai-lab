"""Explicit fixture setup, official stdio serving and reproducible process demo."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    setup = sub.add_parser("init", help="Create a fresh authored synthetic database")
    setup.add_argument("--database", type=Path, required=True)
    serve = sub.add_parser("serve", help="Serve official MCP on stdin/stdout only")
    serve.add_argument("--database", type=Path, required=True)
    serve.add_argument("--audit", type=Path, required=True)
    serve.add_argument("--profile", choices=["alpha-reader", "alpha-analyst", "beta-reader",
                                             "beta-analyst", "alpha-observer"], required=True)
    demo = sub.add_parser("demo", help="Run actual official-client/process and fault checks")
    demo.add_argument("--work-dir", type=Path, required=True, help="Must not already exist")
    demo.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            from .store import initialize_fixture
            print(json.dumps(initialize_fixture(args.database), sort_keys=True))
        elif args.command == "serve":
            from .audit import GatewayAudit
            from .server import run_stdio
            from .service import GatewayService
            from .store import ReadOnlyStore, principal_for_profile
            store = ReadOnlyStore(args.database)
            try:
                audit = GatewayAudit(args.audit)
                service = GatewayService(store, audit, principal_for_profile(args.profile))
                run_stdio(service)
            finally:
                store.close()
        else:
            import anyio
            from .integration import run_demo
            report = anyio.run(run_demo, args.work_dir)
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2,
                                           allow_nan=False) + "\n", encoding="utf-8")
            print(json.dumps({"report": str(args.out), "checks": len(report["observations"]),
                              "protocol": report["protocol_versions"],
                              "source_unchanged": report["source_unchanged"]}))
        return 0
    except (OSError, ValueError) as exc:
        # Operator errors go to stderr, never corrupt stdio JSON-RPC framing.
        print(f"Gateway could not start or finish ({type(exc).__name__}).", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
