"""Render a private offline fictional-usage and modeled-ROI dashboard."""

from __future__ import annotations

import argparse
import json
import os
import stat
from pathlib import Path

from ..costs import CostError, CostLedger, principal_for_profile
from ..costs.contracts import MAX_INPUT, strict_json
from . import build_report, export_report


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise CostError("invalid_input")


def read_scenario(path):
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or info.st_size > MAX_INPUT
        ):
            raise CostError("invalid_input")
        data = bytearray()
        while len(data) <= MAX_INPUT:
            chunk = os.read(fd, min(32768, MAX_INPUT + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(fd)
        if len(data) != info.st_size or (info.st_size, info.st_mtime_ns) != (
            after.st_size,
            after.st_mtime_ns,
        ):
            raise CostError("invalid_input")
        return strict_json(bytes(data))
    except OSError:
        raise CostError("invalid_input") from None
    finally:
        if fd is not None:
            os.close(fd)


def main(argv=None):
    parser = Parser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    render = sub.add_parser("render")
    render.add_argument("--ledger", type=Path, required=True)
    render.add_argument("--profile", required=True)
    render.add_argument("--scenario", type=Path, required=True)
    render.add_argument("--out-dir", type=Path, required=True)
    try:
        args = parser.parse_args(argv)
        assumptions = read_scenario(args.scenario)
        principal = principal_for_profile(args.profile)
        report = build_report(CostLedger(args.ledger), principal, assumptions)
        result = export_report(report, args.out_dir)
        print(json.dumps({"ok": True, "result": result}))
        return 0
    except Exception as exc:  # noqa: BLE001 - no input, path or exception text is exported
        code = (
            exc.code
            if type(exc) is CostError
            and exc.code
            in {
                "invalid_input",
                "invalid_profile",
                "forbidden",
                "capacity_exceeded",
                "resource_busy",
                "storage_unavailable",
                "storage_corrupt",
            }
            else "storage_unavailable"
        )
        print(json.dumps({"ok": False, "error": code}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
