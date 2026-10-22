# Fictional tenant cost ledger: local process evidence

This is an offline accounting experiment with authored usage, fictional USD rates
and independently authored statements. It does not contain provider telemetry,
actual pricing, invoices, payments, measured savings or ROI. Earlier RAG/OCR/MCP
runs are not measured or billed by this study.

The [demo](demo.json) records 31 observations across 33 actual owned Python
processes, with runtime/source/fixture hashes and the true pre-change base HEAD.
The launcher profiles are trusted local policy, **not authentication**. All owned
children were waited/reaped; one fixed experiment-only child was killed after
SQLite commit and before returning stdout. Its exact event retry recovers the
original receipt.

| Initial tenant scenario | Events | Exact USD | Settled USD | Rounding delta |
| --- | --- | --- | --- | --- |
| Alpha | 5 | `0.566` | `0.57` | `0.004` |
| Beta | 3 | `1.018` | `1.02` | `0.002` |

Later Alpha usage changes the live total to `0.581`, settled `0.58`, while the
original statement ID replays its original snapshot and result. The two tenants
share event IDs. Retained cases cover missing/extra rows, line amount, currency,
settled-total mismatch and duplicate statement lines; no FX calculation is made.
Computed reconciliation totals explicitly retain rate-card currency `USD` when
a statement declares another currency.

The concurrent duplicate case has two actual CLI writers returning one durable
receipt: one insert and one replay. This is local idempotency evidence, not an
end-to-end exactly-once guarantee. The lost-receipt fault is deliberately injected
by a fixed private experiment wrapper; it does not explain an observed production
incident or claim automatic retry of arbitrary commands.

## Reproduce

```sh
python -m pip install --no-deps -e .
python -W error::ResourceWarning -m unittest discover -s tests/costs -v
python -m enterprise_ai_lab.costs demo \
  --work-dir .local/cost-rerun --out reports/local/cost-rerun.json
```

Use fresh work and output paths. Receipt IDs and times naturally vary.
The packaged fixture bytes and literal expected totals are fixed; the input
hashes have explicit `data/...` keys in the same `source_sha256` map as runtime
source hashes. These inspectable correctness
fixtures are not a held-out statistical benchmark.

## Journal-admission follow-up

The unchanged JSON is preserved in commit `b04896fb7476268a6840e734aa830b77b776a2e7`
and binds ledger source `278785199682746d10d6cac88f59579206adb28276cc706f763f3064d7389dc5`.
It does **not** exercise the later missing-sidecar guard. A separate deterministic
regression pauses a real writer before commit, then lets SQLite naturally unlink
its journal between another process's existence/stat checks. The original guard
returns `storage_corrupt`; the repair retains the committed usage. The regression
and all 39 cost tests pass on Python 3.10 and 3.13 locally.

The first Linux CI failure involved an unexpected finite child code that the old
assertion did not print. Its exact historical cause remains unknown; the controlled
regression establishes a separate admission race, without an exactly-once claim.

## Harness follow-up

The JSON above also predates the bounded-contention/readiness harness repair.
Controlled real SQLite/CLI tests reproduce a successful first writer and a
`resource_busy` second writer; the old harness reports `demo_failed`. The current
harness retains both initial responses and, only after both children exit, makes
at most one exact replay per initial busy response. Other errors stop the demo.
New runs record the replay/attempt counts and have `31+k` observations and `33+k`
processes for `k` in `0..2`; the unchanged original JSON remains 31/33.

Separate deterministic marker tests pause a real child with zero or 19 bytes
written. The old final-path write exposes those incomplete bytes; the new child
publishes only a closed complete temporary file by atomic replacement. Both are
harness regressions, **not** an explanation of the second Linux CI's undetailed
`demo_failed` envelope. Fixed finite stage/error/return-code diagnostics now help
distinguish future failures without printing child text, input IDs or stderr.
See the [controlled harness tests](../../tests/costs/test_cost_harness.py).

The [contract and exercises](../../docs/cost-attribution.md) describe exact bounds,
aggregate-first half-even rounding, immutable statement snapshots, SQLite storage
tradeoffs and limitations. Schema/digest checks require a trusted host and are
not an externally anchored or administrator-proof audit. Real usage collection,
authenticated access, credits/refunds, tax/FX and billing execution remain outside
this milestone. Recorded execution does not establish the owner's personal study
or human review.
