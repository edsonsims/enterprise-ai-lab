# Fresh reliability case: three distinct retry boundaries

On **2026-10-30**, three separate local experiments ran once from a newly built
**0.14.1 wheel installed outside the source checkout**. They compare an audited
read whose response is lost, an approved local note whose commit precedes its
acknowledgment, and a caller/context-bound redaction map. This advances the second
representative case of roadmap item 15. The fresh operability case and the full
three-case milestone remain pending.

These are **three separate experiments**. There is no connected
privacy-to-MCP write/action pipeline, and no transaction spans their databases.

| Experiment | Fresh observation | Boundary |
| --- | --- | --- |
| Official MCP client and gateway failover | 15 cases, 20 reaped children, 31 audit rows; both source hashes unchanged | A second read is another audited attempt. Permission/audit refusals stop instead of seeking another replica. |
| Temporal approval and local SQLite notes | 29 HTTP observations, five raw SDK history replays, two tenant-local notes; the interrupted primary has one note | Retry the same immutable command after an uncertain acknowledgment; retaining the separate SQLite store is part of effect deduplication. |
| Scoped redaction/restoration | 25 HTTP observations, three downstream requests, three reaped resources and 14 audit rows | Tenant, client and context constrain restoration. A processor failure may leave a bounded expiring map; it does not authorize a new automatic retry. |

The observed whole-demo times were **8.572 s**, **37.811 s** and **3.018 s**,
respectively, including their startup and cleanup. These are individual local
observations, not latency percentiles, service-level objectives or a performance
comparison.

## Runtime and original evidence

The wheel's **113 package files** match the clean runtime source at
[`d4fa3236e047d63ea7d34f6f26debc31513520e6`](https://github.com/edsonsims/enterprise-ai-lab/tree/d4fa3236e047d63ea7d34f6f26debc31513520e6).
Its SHA-256 is
`85742f15e7ad8fbc965a50d97dc0f5bc506b66da1ebf0aa459d3366433ad3c0e`.
Archive metadata can change a rebuilt wheel's hash; compare its package-file
inventory with the source rather than assuming archive-byte reproducibility.

Two fresh Python **3.13.1** environments used the unchanged exact MCP and approval
locks. The approval environment also ran the privacy experiment. All five
existing requirement locks remain unchanged. Setup used a private build
environment with **setuptools 84.2.1**. The initial system interpreter lacked the
build backend, and an initial inventory guard assumed four locks instead of
five; both setup observations were retained and corrected before any experiment.
There was no experiment retry, budget increase or assertion weakening.

The actual local Identity Boundary Lab source is
[`92a6688e6df862934d53201a6ac39d8fba410a52`](https://github.com/edsonsims/identity-boundary-lab/tree/92a6688e6df862934d53201a6ac39d8fba410a52),
with a fresh build reproducing the recorded bridge bytes. Runtime evidence pins
Node **24.19.0**, Temporal CLI **1.9.1** / native server **1.32.0**, package
versions, executable hashes and both exact runtime locks. These demos need no
model assets, make no model/provider call and do not download at runtime. They
use actual loopback services and stdio; **OS-level egress denial was not tested**
for this case.

- [Provenance and portable command templates](provenance.json)
- [Source-to-wheel package inventory](wheel-source-inventory.json)
- [Installed runtime inventory](runtime-inventory.json)
- [MCP report](chaos/demo.json)
- [Approval report](approval/demo.json) and its five [raw histories](approval/histories)
- [Privacy report](privacy/demo.json)

The three reports and five histories are byte-identical public copies of the
new raw artifacts. Actual SDK replay used those raw approval histories. The
public-safety scan inspected all eight JSON artifacts and 22 decoded history
payloads against known private path, bearer and JWT canaries, with zero matches.
Issuer IPC, generated credentials, databases and raw process logs are excluded.
A finite canary scan does not prove absence of all private content or log leaks.

## Verify the recorded contract offline

From the source checkout, with only Python's standard library:

```sh
python scripts/verify_reliability_case.py --directory reports/case-reliability-v1
PYTHONPATH=src python -W error::ResourceWarning -m unittest discover -s tests/case_studies -v
```

The verifier checks the recorded case inventories and semantic boundaries,
artifact/source bindings and exported history payloads. It starts no process,
performs no SDK replay, reads no live database and proves no new execution.
Missing or duplicated cases and inconsistent refusal/retry/effect evidence must
remain failures. The historical inventory and first knowledge-case verifier
retain their separate artifacts and evidence limits.

The recorded bundle passes **453 contract checks**. The complete case-study suite
passes **124 tests**, including **56 new contract tests** for resealed audit,
history, refusal, effect and file-reading corruptions. The original 14 core tests
pass separately. These local results use Python 3.13.1; they do not repeat the
three runtime experiments or establish a different native platform matrix.

## Repeat the actual experiments

Use Python **3.12+**, the exact two runtime locks, the pinned local Temporal
executable and the Identity source above. Follow the
[MCP failover](../../docs/chaos-failover.md),
[approval](../../docs/durable-approval.md) and
[privacy](../../docs/privacy-proxy.md) setup guides. Build a wheel from the
recorded runtime revision, install it with `--no-deps` in fresh environments,
and run outside the source checkout with fresh output parents and work paths:

```sh
"$MCP_CASE_PYTHON" -I -m enterprise_ai_lab.chaos demo \
  --work-dir "$NEW_MCP_WORK" --out "$NEW_REPORT_PARENT/chaos/demo.json"
"$APPROVAL_CASE_PYTHON" -I -m enterprise_ai_lab.approval demo \
  --temporal-cli "$TEMPORAL_CLI" --identity-dir "$IDENTITY_DIR" --node "$NODE_BINARY" \
  --work-dir "$NEW_APPROVAL_WORK" --out "$NEW_REPORT_PARENT/approval/demo.json"
"$APPROVAL_CASE_PYTHON" -I -m enterprise_ai_lab.privacy_proxy demo \
  --identity-dir "$IDENTITY_DIR" --node "$NODE_BINARY" \
  --work-dir "$NEW_PRIVACY_WORK" --out "$NEW_REPORT_PARENT/privacy/demo.json"
```

The shell names denote explicit local paths, not configured credentials. Each
report parent must exist; each work/report path must be new. Portable templates
are labelled separately from the private exact executed commands and receipts.
A new execution writes new evidence and must not overwrite these or older runs.

## What the results permit us to say

The MCP tests use trusted launcher profiles, not authentication. The replicas
share one host and a fixed fictional dataset. An unfinished audit intent does
not prove SQL ran; a durable outcome whose response was lost does not disappear
when a second read succeeds. Cleanup can extend the total elapsed time beyond
the work budget. The experiment does not prove interruption inside executing
SQLite, remote replication, host-failure tolerance or exactly-once behavior.

Approval fixtures use OAuth service credentials, not human login or review. The
effect is a fictional local SQLite note. Alpha and Beta create two distinct
notes; the primary interruption/retry creates one Alpha note. Durable approval,
receipt reconciliation and completed effects remain distinct. Development
Temporal gRPC and the host filesystem are trusted administration. There is no
RAG corpus revision or publication in this approval workflow.

The privacy vault is plaintext SQLite. Expiry is neither encryption nor secure
erasure. The resource log scan covers 156 bytes and its known supported values
and minted credential canaries, not arbitrary future secrets. A fictional name
is intentionally unsupported and passes through. The frozen detector-development
corpus and its errors remain unchanged; this case is no new detector-quality or
compliance measurement.

No customer outcome, production deployment, real professional document,
external notification, package registry release, owner study or human review
is established by these automated experiments.
