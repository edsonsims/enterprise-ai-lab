"""Offline fictional usage ledger. Launcher profiles are trusted policy, not login."""

from __future__ import annotations

import argparse
import json
import os
import stat
from pathlib import Path

from .contracts import MAX_INPUT, CostError, bounded, principal_for_profile, strict_json
from .ledger import CostLedger, initialize


def read_input(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_INPUT:
            raise CostError("invalid_input")
        return strict_json(os.read(fd, MAX_INPUT + 1))
    finally:
        os.close(fd)


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise CostError("invalid_input")


def main(argv=None):
    parser = Parser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--ledger", type=Path, required=True)
    for command in ("append", "get", "list", "summary", "reconcile"):
        child = sub.add_parser(command)
        child.add_argument("--ledger", type=Path, required=True)
        child.add_argument("--profile", required=True)
        if command in ("append", "reconcile"):
            child.add_argument("--input", type=Path, required=True)
        if command == "get":
            child.add_argument("--event-id", required=True)
        if command == "list":
            child.add_argument("--after", type=int, default=0)
            child.add_argument("--limit", type=int, default=100)
        if command == "summary":
            child.add_argument("--start", required=True)
            child.add_argument("--end", required=True)
    demo = sub.add_parser("demo")
    demo.add_argument("--work-dir", type=Path, required=True)
    demo.add_argument("--out", type=Path, required=True)
    try:
        args = parser.parse_args(argv)
        if args.command == "demo":
            from .experiment import run_demo

            if args.out.exists() or args.out.is_symlink():
                raise CostError("invalid_input")
            report = bounded(run_demo(args.work_dir))
            args.out.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False)
                    + "\n"
                )
            value = {
                "observations": len(report["observations"]),
                "synthetic_only": True,
            }
        elif args.command == "init":
            initialize(args.ledger)
            value = {"initialized": True}
        else:
            principal = principal_for_profile(args.profile)
            ledger = CostLedger(args.ledger)
            if args.command == "append":
                value = ledger.append(principal, read_input(args.input))
            elif args.command == "get":
                value = ledger.get(principal, args.event_id)
            elif args.command == "list":
                value = ledger.list(principal, after=args.after, limit=args.limit)
            elif args.command == "summary":
                value = ledger.summary(principal, args.start, args.end)
            else:
                value = ledger.reconcile(principal, read_input(args.input))
        print(
            json.dumps(
                {"ok": True, "result": bounded(value)},
                ensure_ascii=False,
                allow_nan=False,
            )
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - fixed CLI error envelope
        code = exc.code if isinstance(exc, CostError) else "storage_unavailable"
        print(json.dumps({"ok": False, "error": code}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
