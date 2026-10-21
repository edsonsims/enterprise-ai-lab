"""Run the bounded offline MCP chaos experiment in a fresh directory."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    demo = commands.add_parser('demo')
    demo.add_argument('--work-dir', type=Path, required=True, help='Must not exist')
    demo.add_argument('--out', type=Path, required=True, help='Must not exist')
    args = parser.parse_args(argv)
    try:
        # Refuse an existing result before any experiment mutation.
        if args.out.exists():
            raise ValueError()
        import anyio
        from .experiment import run_demo
        report = anyio.run(run_demo, args.work_dir)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open('x', encoding='utf-8') as stream:
            stream.write(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + '\n')
        print(json.dumps({'completed': True, 'observations': len(report['observations']),
                          'processes': report['actual_processes'], 'source_unchanged': report['source_unchanged']}))
        return 0
    except Exception:
        print('Chaos demo could not complete; inspect the documented bounds and private local state.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
