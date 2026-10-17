"""Offline demonstration and external retrieval prediction evaluation."""
import argparse
import json
import sys

from .core import ValidationError, predict, read_jsonl, synthetic_dataset, write_bundle


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="run synthetic ACL-prefilter and unsafe-global experiments")
    demo.add_argument("--output", default="reports/demo")
    demo.add_argument("--seed", type=int, default=42)
    demo.add_argument("--k", type=int, default=3)
    evaluate = commands.add_parser("evaluate", help="evaluate externally supplied JSONL predictions")
    for name in ("documents", "cases", "predictions", "output"):
        evaluate.add_argument(f"--{name}", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "demo":
            documents, cases = synthetic_dataset(args.seed)
            safe = write_bundle(args.output, documents, cases, predict(documents, cases, args.k),
                                label="acl_prefilter", seed=args.seed, k=args.k)
            unsafe = write_bundle(f"{args.output}/unsafe_global_baseline", documents, cases,
                                  predict(documents, cases, args.k, unsafe_global=True),
                                  label="UNSAFE_GLOBAL_LAB_ONLY", seed=args.seed, k=args.k)
            print(json.dumps({"output": args.output, "safe_query_leakage": safe["metrics"]["query_leakage_rate"]["value"],
                              "unsafe_query_leakage": unsafe["metrics"]["query_leakage_rate"]["value"]}))
        else:
            report = write_bundle(args.output, read_jsonl(args.documents), read_jsonl(args.cases),
                                  read_jsonl(args.predictions), label="external_predictions")
            print(json.dumps({"output": args.output, "case_count": report["case_count"]}))
    except (ValidationError, OSError, UnicodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0
