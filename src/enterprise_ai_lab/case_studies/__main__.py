"""Inspect fixed historical case-study evidence without executing any demo."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import AuditError, inspect, write_report


class Parser(argparse.ArgumentParser):
    def error(self, _message):
        raise AuditError("invalid_arguments")


def main(argv=None):
    parser = Parser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
    command = sub.add_parser("inspect")
    command.add_argument("--repository", required=True, type=Path)
    command.add_argument("--out", type=Path)
    try:
        args = parser.parse_args(argv)
        report = inspect(args.repository)
        if args.out is not None:
            write_report(report, args.out)
        print(json.dumps(report, sort_keys=True, allow_nan=False))
        return 0 if report["ok"] else 1
    except AuditError as exc:
        print(json.dumps({"ok": False, "error": exc.code}))
        return 2
    except Exception:  # no arbitrary exception text, private path or input is exported
        print(json.dumps({"ok": False, "error": "audit_unavailable"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
