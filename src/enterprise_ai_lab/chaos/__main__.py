"""Run the bounded offline MCP chaos experiment in a fresh directory."""
from __future__ import annotations

import argparse
import json
import sys
from functools import partial
from pathlib import Path

from .diagnostics import DemoDiagnostics, format_failure, validate_failure


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    demo = commands.add_parser('demo')
    demo.add_argument('--work-dir', type=Path, required=True, help='Must not exist')
    demo.add_argument('--out', type=Path, required=True, help='Must not exist')
    args = parser.parse_args(argv)
    diagnostic = DemoDiagnostics()
    failures = []

    def remember(record):
        failures[:] = [validate_failure(record)]

    try:
        # Refuse an existing result before any experiment mutation.
        diagnostic.stage('output_refusal')
        if args.out.exists():
            raise ValueError()
        diagnostic.stage('demo_run')
        import anyio

        from .experiment import run_demo
        report = anyio.run(partial(run_demo, args.work_dir, _on_failure=remember))
        diagnostic.stage('report_write')
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open('x', encoding='utf-8') as stream:
            stream.write(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + '\n')
        print(json.dumps({'completed': True, 'observations': len(report['observations']),
                          'processes': report['actual_processes'], 'source_unchanged': report['source_unchanged']}))
        return 0
    except Exception as exc:
        try:
            print('Chaos demo could not complete; inspect the documented bounds and private local state.', file=sys.stderr)
            record = failures[-1] if failures else diagnostic.failure(exc)
            print(format_failure(record), file=sys.stderr)
        except Exception:
            pass  # Diagnostic delivery must not replace the existing exit status.
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
