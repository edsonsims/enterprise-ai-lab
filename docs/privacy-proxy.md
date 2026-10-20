# Redact before a local tool; restore inside the same identity boundary

This study asks whether a small, auditable transformation can hide selected
substrings from a downstream tool while retaining a strictly scoped way to put
them back. The detector is deterministic. The HTTP services, OAuth signatures,
SQLite transactions, process restart and expiry are actual implementations.
The downstream demo is a local string transformer; it calls no model or external
provider. All recorded input is fictional.

## Reproduce

Use Python **3.12+**, Node **24**, and a sibling Identity Boundary Lab checkout
at the revision pinned by the privacy CI job. Install the existing exact
`requirements-approval.lock`; this reuses its HTTP/JWT stack without starting
Temporal. The original RAG, ingestion, approval and MCP locks are unchanged.

```sh
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements-approval.lock
.venv/bin/python -m pip install --no-deps -e .
(cd ../identity-boundary-lab && npm ci --ignore-scripts && npm run build)

.venv/bin/python -m enterprise_ai_lab.privacy_proxy demo \
  --identity-dir ../identity-boundary-lab --node node \
  --work-dir .local/privacy-demo --out reports/local/privacy-demo.json

.venv/bin/python -m enterprise_ai_lab.privacy_proxy evaluate \
  --out reports/local/privacy-evaluation.json
.venv/bin/python -m enterprise_ai_lab.privacy_proxy verify-report \
  reports/local/privacy-evaluation.json

RUN_PRIVACY_INTEGRATION=1 IDENTITY_LAB_DIR="$PWD/../identity-boundary-lab" \
NODE_BINARY=node .venv/bin/python -m pytest tests/privacy -q
```

Choose a new work directory on each demo invocation. Existing private vaults are
preserved. Runtime services never download assets. Dependency installation is
separate setup; neither a model, OCR engine nor Temporal server is needed here.
The [frozen public run](../reports/privacy-v1) includes full synthetic responses,
the requests actually seen by the local processor and per-case evaluation errors.

## Three operations, one scope

The resource verifies a signed RS256 JWT against startup discovery/JWKS from the
local Identity Boundary Lab issuer. Its dedicated audience is
`urn:identity-boundary:privacy`; a RAG or approval token is insufficient.
Tenant and client ID come from verified claims, never body fields or headers.

| POST path under `/v1/tenants/{tenant}/privacy` | Exact body | Required scope and role |
| --- | --- | --- |
| `/redactions` | `{"text":"..."}` | `privacy:redact` AND `redactor` |
| `/restorations` | `{"context_id":"32 lowercase hex characters","text":"..."}` | `privacy:restore` AND `restorer` |
| `/process` | `{"text":"..."}` | Both scopes AND both roles |

Redaction returns a generated context ID, logical expiry, transformed text and
exact Unicode code-point spans. Each occurrence receives an independent random
`[[PII:<32 lowercase hex characters>]]` placeholder. Even equal substrings in two
contexts receive different tokens. A token can be restored only by the same
**tenant AND OAuth client ID AND context**. A fresh access token for that client
can reopen the unexpired context after a resource restart; the demo keeps the
issuer running. There is no cross-client handoff, browser session or issuer
persistence claim.

Restoration parses placeholders once. Unknown, malformed, expired or copied
context tokens reject the whole operation before returning any plaintext. Output
expansion is bounded; original input containing the reserved `[[PII:` prefix is
rejected. A downstream tool may remove/reorder/repeat authorized placeholders;
this service checks token provenance, not the semantic validity of its answer.

`/process` persists the map, sends only `{"text": redacted_text}` to one
operator-configured literal loopback origin at `/process`, validates its bounded
JSON response, rechecks JWT expiry, and restores within the original context.
No access token, identity header, context ID or source-span inventory is forwarded.
Caller-selected targets, redirects and inherited HTTP proxy settings are disabled.
Its processor HTTP handling uses 2-second I/O timeouts and a checked 3-second
elapsed budget; an in-flight read may finish at its I/O timeout after the elapsed
budget. This is a bounded local experiment, not a hard real-time guarantee.

To run just the resource against an already running compatible local issuer:

```sh
.venv/bin/python -m enterprise_ai_lab.privacy_proxy serve \
  --issuer http://127.0.0.1:9100 --vault .local/private-vault/maps.sqlite3 \
  --processor http://127.0.0.1:9200 --port 8091
```

The immediate vault directory must be owned and mode `0700`; the database and
admitted journal must be owned, regular, single-link files of mode `0600`.
The processor endpoint must implement the exact contract above. Omitting
`--processor` keeps redaction/restoration available and makes `/process` return
502. Access tokens and generated fixture secrets stay in private process memory
and Authorization headers, not shell arguments or exported reports.

## Detection is intentionally incomplete

V1 recognizes a narrow ASCII mailbox/domain format, `+1` phone formats, and
13–19 digit card-like numbers passing Luhn, with bounded spaces/hyphens. A Luhn
match does not establish a real payment card. Overlaps use explicit precedence:
email, card-like number, then phone; longer spans and offsets break ties.
No learned detector, named-entity recognition, classification of real people,
payment execution or remote provider integration is implemented.

The 36-case development fixture was frozen before scoring; its SHA-256 is
`38974426ef1c84ae6899de815164df51e72cbeface00dc4abc7015e36fbf9279`.
Gold labels stay outside `detect(text)`. Exact-span matching requires equal kind,
start and end; all offsets are Python Unicode code points. Every case remains
in the result. Invalid predictions count as empty, retaining gold in recall.

| Frozen development denominator | TP | FP | FN | Precision | Recall |
| --- | --- | --- | --- | --- | --- |
| Supported kinds, including an unhandled quoted mailbox | 28 | 1 | 1 | 28/29 | 28/29 |
| All annotated spans | 28 | 1 | 12 | 28/29 | 28/40 |

One fictional public contact is an unannotated context-dependent false positive;
one quoted mailbox is missed. Eleven names, addresses, local-format phones and
Unicode emails are outside the emitted labels and remain misses in the all-span
denominator. Their 0/11 coverage follows from this declared output vocabulary;
it is not a general privacy-detection estimate. The demo deliberately forwards a
fictional name while removing its three supported substrings.

This is a **development fixture, not a held-out benchmark**. No generalized
accuracy, anonymity, compliance, model-quality or real-document protection claim
follows. Broader formats or prompt/detector changes need a new development
version and an untouched evaluation set. `verify-report` checks corpus/source
hashes, reruns the deterministic detector and recomputes counts; it cannot
authenticate self-recorded timestamps or platform metadata. Historical reports
require their recorded source checkout.

## Persistence, bounds and failure behavior

SQLite uses parameterized statements, DELETE journaling, foreign keys and one
bounded transaction per operation. Scope lookup, restoration, capacity and audit
commit together. A trigger/storage failure rolls back and returns a finite error,
not a partial result. Symlink/hardlink/replaced-path checks run at construction
and each operation. This assumes a trusted operator, host, filesystem and local
processor; it is not a sandbox against a hostile same-user administrator.

Default limits are 32 KiB UTF-8 input/output, 64 KiB HTTP bodies, 3 seconds for
body receipt, nonblocking single-operation admission, 50 ms SQLite busy waits,
120-second contexts (maximum 600), 256 live contexts, 4,096 mapped occurrences,
131,072 permanently reserved IDs, 10,000 audit rows and a 32 MiB database cap.
Expiry cleanup processes at most 32 contexts per call. Boundary tests cover
collisions, tombstones, repeated values, Unicode offsets, restoration expansion,
actual SQLite locks, concurrency, corrupt databases, private files and rollback.

The audit records only sequence/action/outcome/count for vault operations. It
contains no source text, values, IDs, tokens or bearer credentials. Authentication
and parser rejections before the vault are not audit events. This audit is not
hash-linked or externally anchored. Exhausted audit/tombstone capacity fails
closed; there is no automatic reset or truncation. Explicitly retire the private
vault and choose a fresh path when appropriate; that invalidates old contexts.

Mapped substrings are **plaintext in a private local database**. Expiry is logical
deletion during bounded cleanup; SQLite pages, journals and backups may retain
bytes. There is no encryption, secure physical erasure or retention policy.
Do not use this fixture as a privacy/compliance control for actual private data.

Redaction is not idempotent: a retry creates a new context. Restore can repeat
until expiry. If the processor fails, the already committed map remains until
expiry/capacity cleanup; there is no transaction across HTTP and SQLite. An
unknown response token withholds restoration but cannot undo what the processor
already received. Responses use `Cache-Control: no-store`; errors and CLI failures
use finite text without echoing input. The demo scans actual resource logs for
its known supported substrings and minted credentials; that finite scan does not
prove absence of every possible leak. The raw private issuer IPC is excluded.

## Exercises

1. Reproduce the quoted-mailbox miss and public-contact false positive. Explain
   why exact pattern recognition cannot decide whether an address is private.
2. Restore with another client in the same tenant, then restart only the resource
   and retry as the original client. Explain both outcomes.
3. Hold a real SQLite write lock, observe the bounded 503, release it and retry
   the same restore. Contrast this with retrying redaction or downstream work.
4. Force a random-ID collision and exhaust audit capacity in an isolated test
   vault. Explain why neither active mappings nor tombstones are silently dropped.
5. Design a separate development split for a broader detector. Keep this frozen
   report unchanged and specify a genuinely untouched later evaluation protocol.
