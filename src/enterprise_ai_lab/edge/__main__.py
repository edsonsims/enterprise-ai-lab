"""Explicit local-only CLI; no dependency installation or model bootstrap."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .contracts import EdgeError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="trusted operator stdio inference worker")
    serve.add_argument("--model-dir", required=True)
    serve.add_argument("--threads", required=True, type=int, choices=(1, 4))
    demo = commands.add_parser(
        "demo", help="two fresh CPU worker measurements, no downloads"
    )
    demo.add_argument("--model-dir", required=True)
    demo.add_argument("--out", required=True)
    verify = commands.add_parser(
        "verify-report", help="validate recorded data without importing a model"
    )
    verify.add_argument("--report", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            from .worker import serve as run

            return run(args.model_dir, args.threads)
        from .experiment import measure, read_report, verify_report, write_report

        if args.command == "verify-report":
            result = verify_report(read_report(args.report))
        else:
            out = Path(args.out)
            # Refuse existing output before starting any expensive model process.
            if out.exists() or out.is_symlink():
                raise EdgeError("output_exists")
            if not out.parent.is_dir() or out.parent.is_symlink():
                raise EdgeError("storage_unavailable")
            report = measure(args.model_dir)
            result = verify_report(report)
            result["report_sha256"] = write_report(out, report)
            if result["terminal_errors"]:
                print(
                    json.dumps(
                        {"ok": False, "error": "worker_failed", "result": result},
                        sort_keys=True,
                    )
                )
                return 2
        print(json.dumps({"ok": True, "result": result}, sort_keys=True))
        return 0
    except Exception as exc:  # noqa: BLE001 - retain only fixed finite metadata.
        code = exc.code if type(exc) is EdgeError else "worker_failed"
        print(json.dumps({"ok": False, "error": code}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
