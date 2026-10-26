<picture>
  <source media="(max-width: 600px)" srcset="assets/readme-header-mobile.svg">
  <img src="assets/readme-header.svg" alt="Enterprise AI Lab — replayable experiments in permission-aware retrieval" width="1200">
</picture>

A question should reach the right documents before it reaches a model. This lab
connects a real local OAuth issuer to dense vector retrieval, tenant/role filters
and cited answers. Public PDFs now enter through native extraction or actual
OCR, with page-level source spans and explicit permissions. A connected
publication path keeps proposed documents outside search until their exact
content and access policy receive a durable approval. The original
synthetic experiments remain available; HTTP, cryptography, embeddings and
persistence are real implementations.

The original lexical retrieval evaluator is still available below. It is a
separate controlled experiment, with its original reports preserved.

[PDF/OCR demo](#ingest-public-pdfs-and-scans) · [Identity → RAG design](docs/identity-rag.md) ·
[Recorded public run](reports/pdf-v1/demo.json) · [Connected synthetic demo](reports/rag-v1) ·
[Durable approval demo](#wait-for-approval-survive-a-restart) ·
[Approve → publish → query](#approve-a-document-before-it-enters-search) ·
[Browser review room](#open-the-source-and-leave-a-decision) ·
[SQLite MCP gateway](#give-a-tool-the-rows-it-can-read) ·
[Read-only failover](#lose-a-connection-without-bypassing-a-refusal) ·
[Fictional cost ledger](#count-usage-before-rounding-cost) ·
[Activity and modeled ROI](#separate-activity-from-modeled-benefit) ·
[Offline core wheel](docs/offline-bundle.md) ·
[Local CPU inference](#measure-startup-and-warm-inference) ·
[Local configuration desk](#review-a-configuration-change-and-keep-its-receipt) ·
[Scoped redaction proxy](#redact-before-a-local-tool-and-restore-in-context) ·
[Original retrieval study](docs/study-01.md) · [Roadmap](ROADMAP.md)

## Run the connected RAG demo

Use **Python 3.12+** (3.13 for the recorded run) and **Node.js 24**. The locked RAG
runtime is optional; the original evaluator keeps its dependency-free setup.
From this repository:

```sh
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements-rag.lock
.venv/bin/python -m pip install --no-deps -e .

# Use a sibling checkout; skip cloning if it already exists.
git clone https://github.com/edsonsims/identity-boundary-lab ../identity-boundary-lab
(cd ../identity-boundary-lab && npm ci --ignore-scripts && npm run build)

# Explicit one-time download of pinned public embedding assets, about 91 MB.
.venv/bin/python -m enterprise_ai_lab.rag.models bootstrap --model-dir .local/models/minilm

.venv/bin/python -m enterprise_ai_lab.rag demo \
  --identity-dir ../identity-boundary-lab \
  --model-dir .local/models/minilm --store .local/rag-demo \
  --out reports/local/rag-demo.json
```

The command owns two separate local processes: Identity Boundary Lab's actual
`oidc-provider` issuer and this Python resource API. Tokens move through a private
child-process pipe and HTTP Authorization headers; they are not saved in reports.
The resource verifies the signature, issuer, **RAG audience**, expiry and scope
before constructing the vector filter from signed tenant/role claims.

Observed outcomes from the extractive demo:

| Request | Result |
| --- | --- |
| Alpha reader asks the processing limit | 200 · **20 documents** · cites `alpha-onboarding` |
| Alpha analyst asks the reserve buffer | 200 · **18 percent** · cites `alpha-reserve` |
| Alpha reader asks for the restricted reserve | 200 · abstains · no citations |
| Token requests another tenant, lacks the access role or lacks `rag:query` | 403 |
| Missing token or token for the reports audience | 401 |
| Caller tries to override tenant/roles in headers or JSON | 400 / 422 |
| Python resource restarts without re-seeding | Same persisted answer and cited content |

The store contains six authored documents split into eight chunks. Retrieval uses
**384-dimensional MiniLM ONNX embeddings and persistent Qdrant local mode**.
The Qdrant query applies `tenant AND any allowed role` before top-k; a second
payload check rejects unexpected results. No globally retrieved context is used
as a fallback.

The default answer step is an explicit **extractive baseline**, not a neural
language model. It copies a matching authorized sentence or abstains. The
[recorded output](reports/rag-v1/extractive.json) includes the complete responses.
Cosine scores may change slightly after restart; tests preserve exact IDs, text,
ACLs and answers with a `1e-6` score tolerance.

### Run actual local generation

The optional Qwen adapter performs real CPU generation from those same filtered
chunks. Install its recorded runtime and explicitly download the pinned public
checkpoint:

```sh
.venv/bin/python -m pip install -r requirements-generator.lock
.venv/bin/python -m enterprise_ai_lab.rag.neural bootstrap --cache-dir .local/models/qwen
```

That command prints a verified `model_dir`. Pass that path to a new demo run:

```sh
.venv/bin/python -m enterprise_ai_lab.rag demo \
  --identity-dir ../identity-boundary-lab --model-dir .local/models/minilm \
  --store .local/rag-qwen --out reports/local/rag-qwen.json \
  --generator qwen --generator-model-dir /path/printed/by/bootstrap
```

Model files stay outside Git. Serving loads verified local files and performs no
model download. A failed or malformed generation returns an error, with no silent
extractive fallback. Empty authorized context skips generation entirely.

Generated evidence must name an authorized retrieved chunk and quote it exactly.
That checks literal provenance, **not semantic faithfulness**. Answers still
require human review. Read the [actual neural observations](reports/rag-v1) and
[design limits](docs/identity-rag.md) before interpreting this small fixture.

### Exercise the integration boundaries

```sh
RUN_RAG_INTEGRATION=1 \
RAG_MODEL_DIR="$PWD/.local/models/minilm" \
IDENTITY_LAB_DIR="$PWD/../identity-boundary-lab" \
.venv/bin/python -m pytest tests/rag -q
```

These tests exercise actual issuer/resource HTTP, signatures, model embeddings,
vector filtering, role restrictions, expiry, issuer outage and store restart.
Other focused tests inject faults into real local persistence, verify admission
failures never call retrieval, and check malformed generated citations. They are
identified separately from actual model-generation observations.

Every local index mutation writes a pending marker first. A failed or interrupted
update makes the running process and a restarted process refuse that store,
until it is rebuilt from trusted source documents. That favors refusing an
incomplete permission update over serving stale access metadata.

[Protocol, HTTP errors and study exercises](docs/identity-rag.md) ·
[Model/fixture provenance](src/enterprise_ai_lab/rag/data/PROVENANCE.md)

## Ingest public PDFs and scans

The new demo uses two selected pages from public NIST publications. One is
native text; the other is a labelled image-only derivative that must pass through
**PDFium and Tesseract**. The unchanged originals, derivative, hashes, page mapping
and [source rights](public-data/pdf-v1/README.md) are checked in.

Use the connected setup above, **Tesseract 5 with English data**, and install
both compatible lockfiles together:

```sh
.venv/bin/python -m pip install -r requirements-rag.lock -r requirements-ingestion.lock

.venv/bin/python -m enterprise_ai_lab.rag pdf-demo \
  --manifest public-data/pdf-v1/ingestion.json \
  --identity-dir ../identity-boundary-lab \
  --model-dir .local/models/minilm --store .local/pdf-demo \
  --out reports/local/pdf-demo.json
```

Use a fresh store: the provenance-bearing index is **schema 2**, and old index
fingerprints are deliberately rejected. The [complete PDF guide](docs/pdf-ingestion.md)
also includes standalone setup and an `ingest` command without the HTTP demo.

| Material | Recorded origin | Authored local access |
| --- | --- | --- |
| *Zero Trust Architecture* | Original PDF page 15 · native text | Alpha reader; Beta reader |
| *AI RMF Core* | Raster page 1 → original PDF page 25 · actual OCR | Alpha analyst; Beta reader |

Ask **“How is access to individual enterprise resources granted?”** or
**“What functions is the Core composed of?”**. The demo uses actual issuer/resource HTTP,
signed tokens, MiniLM vectors and persisted Qdrant with the extractive answer
baseline. The [complete recorded responses](reports/pdf-v1/demo.json) keep the
permission controls and the sources behind each quoted answer.

The recorded generic question “What are the four functions?” abstains despite
the analyst's permitted source. That failure remains in the report: permission
filtering does not fix question wording or retrieval/answer relevance.

Each chunk carries the original URL, original and processed PDF hashes/pages,
extraction method, canonical page-text hash and exact character span. OCR adds
word spans, pixel boxes and uncalibrated confidence diagnostics. Tenant and role
grants come from the trusted local manifest; PDF metadata cannot grant access.
The same prefilter protects retrieval, generator context and citations.

Re-ingestion replaces that tenant/source/page's content and ACLs. It extracts
the whole incoming batch before writing; an incomplete index mutation refuses
reads and restart until rebuilt. Omitted pages **are not deletions**. There is no
public upload endpoint or live entitlement synchronization.

```sh
RUN_RAG_INTEGRATION=1 \
RAG_MODEL_DIR="$PWD/.local/models/minilm" \
IDENTITY_LAB_DIR="$PWD/../identity-boundary-lab" \
.venv/bin/python -m pytest tests/rag tests/ingestion -q
```

This is a two-page integration fixture, with one clean raster OCR page. It does
not establish OCR accuracy on damaged scans, handwriting, difficult layouts or
other languages. Native reading order and OCR text can lose layout; literal
citations still require interpretation and human review.

## Wait for approval, survive a restart

The next study adds a durable gate for one small action: publish a local note.
It uses actual **Temporal**, a verified OAuth reviewer identity and a separate
SQLite sink. The approval binds the exact note, tenant, requester and deadline.
A rejected or expired request cannot run the note activity.

The process demo restarts the persistent server and HTTP API, then kills a
worker after the note commits but before its acknowledgement. Recovery checks
that the matching local effect stays a single row. Captured histories also run
through the SDK Replayer. Approval and successful publication are separate states.

Use Python 3.12+, Node 24, and the [approval setup](docs/durable-approval.md),
including `requirements-approval.lock` and the explicit pinned CLI bootstrap:

```sh
.venv-approval/bin/python -m enterprise_ai_lab.approval demo \
  --temporal-cli .local/temporal-bin/temporal \
  --identity-dir ../identity-boundary-lab --node node \
  --work-dir .local/approval-demo \
  --out reports/local/approval-demo.json
```

The reviewer is a **scripted service identity**, not a logged-in person. The
notification is local; no external message is sent. The development Temporal
endpoint trusts local administrators, so HTTP authentication is not a production
security boundary for gRPC. Read the [protocol, tradeoffs and exercises](docs/durable-approval.md)
and [recorded process run](reports/approval-v1/demo.json).

## Approve a document before it enters search

The [connected publication demo](docs/approved-publication.md) joins those
studies: stage a public native/OCR page outside the index, request review, then
publish only after an independent OAuth reviewer accepts its immutable
snapshot. The approved content includes the tenant, roles, text and full source
provenance. RAG queries keep the existing tenant AND role prefilter and cited
extractive answer.

Use the pinned model and Temporal CLI from the setups above, **Tesseract 5 with
English data**, and all three compatible locks in one Python 3.12+ environment:

```sh
.venv/bin/python -m pip install \
  -r requirements-rag.lock -r requirements-ingestion.lock -r requirements-approval.lock

.venv/bin/python -m enterprise_ai_lab.publication demo \
  --temporal-cli .local/temporal-bin/temporal \
  --identity-dir ../identity-boundary-lab --node node \
  --model-dir .local/models/minilm \
  --manifest public-data/pdf-v1/ingestion.json \
  --work-dir .local/publication-demo \
  --out reports/local/publication-demo.json
```

Pending, rejected and expired candidates remain outside search. A replacement
waits for its own approval while the previously approved content stays live.
Revision checks reject stale publications; an exact activity redelivery returns
the saved effect without restoring an older document or ACL. A pending journal
blocks reads while an exact retry can finish a recognized write gap. A partial
vector mutation keeps its original marker and requires a fresh store rebuild.

The [recorded process evidence](reports/publication-v1) distinguishes actual
OAuth, Temporal, native extraction/OCR, embeddings and persisted vectors from
controlled failures and authored policy. The reviewer remains a **scripted
service identity**, not a person's review. This gate applies to the connected
publication resource; the standalone trusted `rag ingest` study still has its
own direct operator path. That recorded service-identity study keeps its original
scope. The browser review extension below adds local user sign-in; production administration and
live permission synchronization remain future work.

## Open the source and leave a decision

The [review room](docs/human-review.md) adds a real local browser login and a
place to compare permitted PDF pages with their extracted words, chunk spans
and access rules. A reviewer can approve or reject the exact snapshot; the UI
then shows durable acceptance and completed RAG publication separately.

The account's tenant and roles protect the queue, text, PDF previews and audit.
An approver who can read the native page but lacks the OCR page's analyst grant
cannot decide the mixed batch. The independent analyst reviewer can inspect
both. Previews contain **one permitted page**, including an original-page view
for comparison with the OCR derivative.

Use the combined Python/Node/Tesseract setup above and the pinned browser issuer
from the [complete guide](docs/human-review.md):

```sh
.venv/bin/python -m enterprise_ai_lab.review demo \
  --temporal-cli .local/temporal-bin/temporal \
  --identity-dir ../identity-boundary-lab --node node \
  --model-dir .local/models/minilm \
  --manifest public-data/pdf-v1/ingestion.json \
  --work-dir .local/review-demo \
  --out reports/local/review-demo.json
```

For a rendered Chromium check, explicitly run `npm ci --ignore-scripts` and
`npx playwright install chromium`, then add `--browser-test` with a fresh work
directory. Add `--stay-open` to leave the interface running for manual use;
the local command prints its URL and the private generated account-file path.
Do not publish that file or reuse a real password.

The portal uses Code + PKCE and short-lived user tokens stored in server memory,
behind an opaque HttpOnly browser cookie. It checks CSRF and origin on decisions.
The local audit links intents to durable receipts and observed publication;
portal restart requires login again but preserves workflow/audit state.
[Design and exercises](docs/human-review.md) · [Run evidence](reports/review-v1) ·
[Browser protocol](tests/review/browser-protocol.md)

Automated fixture logins and clicks are **not a person's review or owner study**.
The audit is local, not externally tamper-proof, and the development Temporal
administration endpoint remains trusted. External SSO, production HTTPS, key
rotation and live entitlement synchronization remain planned.

## Give a tool the rows it can read

The [MCP gateway study](docs/sqlite-mcp-gateway.md) connects the official Python
MCP SDK to a read-only SQLite database of **fictional work orders**. An actual
stdio client discovers the allowed tools, reads tenant-bound records and leaves
a durable metadata-only audit. The trusted local launcher chooses the profile;
this is a local policy experiment, not remote authentication.

Use Python **3.12+** on macOS/Linux. This small demo needs no model, Node server,
credentials or external system:

```sh
python3.13 -m venv .venv-mcp
.venv-mcp/bin/python -m pip install -r requirements-mcp.lock
.venv-mcp/bin/python -m pip install --no-deps -e .
.venv-mcp/bin/python -m enterprise_ai_lab.mcp_gateway demo \
  --work-dir .local/gateway-demo --out reports/local/gateway-demo.json
.venv-mcp/bin/python -m pytest tests/gateway -q
```

| Call | Observed behavior |
| --- | --- |
| Alpha reader lists two rows per page | `0001, 0003` → `0005, 0006` → `0008, 0009`; restricted rows never consume the page |
| Alpha and Beta ask for `WO-0001` | Different records from their own tenant |
| Reader requests analyst diagnostics | Tool hidden from catalog; direct call also refused |
| Caller adds tenant, role or SQL arguments | Audited refusal; no source data |
| Source is locked / audit cannot commit | Bounded tool error / data withheld |
| Client reconnects in a fresh server process | Same source result; prior audit chain preserved |

Tenant **and** row roles filter inside SQL before ordering and pagination. The
source also has a read-only connection and an operation authorizer. Audit intent
commits before the query, and outcome commits before the response. A timeout
does not prove a read never ran; the tests retain that distinction and verify
same-client recovery and actual process cleanup.

The [recorded run](reports/gateway-v1) includes negotiated modern/legacy
protocols, full synthetic responses, runtime fingerprints, file hashes and
labelled fault probes. SQLite is the implemented connector; Oracle/SAP, remote
IAM and approved write tools remain planned. The local audit has no external
anchor against a host administrator.

## Lose a connection without bypassing a refusal

The [local chaos study](docs/chaos-failover.md) sends actual official MCP reads to
one of two fixed copies of the synthetic work-order fixture. It kills a process
after its durable outcome, stalls a completed read, loses a connection and
exhausts a total deadline. One explicit read-only failover is allowed; permission,
audit, validation and cancellation failures remain terminal.

```sh
.venv-mcp/bin/python -m enterprise_ai_lab.chaos demo \
  --work-dir .local/chaos-demo --out reports/local/chaos-demo.json
.venv-mcp/bin/python -m pytest tests/chaos -q
```

Use the existing `requirements-mcp.lock` setup and a fresh work directory. The
[recorded run](reports/chaos-v1) keeps both attempts and surviving audit intents,
source hashes, observed/reaped children and measured timings. A deadline stops
accepting work; it does not promise rollback or hard real-time process cleanup.
This is a fixed local synthetic study, with no remote system or write tool.

## Count usage before rounding cost

The cost study keeps authored usage once, attributes it to the selected tenant
and reconciles an independent fictional statement against an immutable snapshot.
It uses real SQLite/CLI processes and exact Decimal arithmetic. Prices and usage
are scenario inputs; earlier RAG/OCR/MCP runs are not measured or billed here.

```sh
python -m enterprise_ai_lab.costs demo \
  --work-dir .local/cost-demo --out reports/local/cost-demo.json
python -m unittest discover -s tests/costs -v
```

Alpha's initial exact `0.566` settles to `0.57`; Beta's `1.018` settles to `1.02`.
The demo retains missing/extra/amount/currency/rounding mismatches, stable replay,
concurrent duplicate writers and recovery after a child commits without returning
its receipt. Launcher profiles enforce local tenant/role policy; they are not login.

[Accounting contract and exercises](docs/cost-attribution.md) ·
[Recorded fictional run](reports/costs-v1)

## Separate activity from modeled benefit

The activity dashboard reads one consistent tenant-local cost snapshot. It shows
fictional event counts, distinct usage request IDs and exact ledger charges beside
an explicitly assumed labor-benefit model. A request ID is not a completed task;
the dashboard does not infer successful work or actual customer health.

```sh
python scripts/health_demo.py \
  --work-dir .local/health-demo --out reports/local/health-demo.json
python -m unittest discover -s tests/health -v
```

Open `.local/health-demo/alpha/index.html` locally. The self-contained page needs
no server, model, external assets or network. Its 27 sensitivity combinations use
exact rational arithmetic and declared low/base/high assumptions; those ranges
are not statistical confidence intervals. The authored base scenario models
`5.00` gross benefit against `4.83` total cost, giving `0.17` net and `3.52%` ROI.
These are fictional scenario calculations, not measured savings or a business result.

The actual CLI story checks tenant isolation, observer denial, identical repeated
exports, a later ledger append and refusal to overwrite the earlier snapshot.
Exports are private plaintext files; copying them grants access to their contents.
Trusted launcher profiles remain separate from authentication.

[Model, provenance and exercises](docs/activity-roi.md) ·
[Authored inputs](public-data/health-v1) · [Recorded local run](reports/health-v1)

## Measure startup and warm inference

The local inference node runs the existing pinned Qwen3-0.6B adapter in a fresh
CPU worker. A stdio JSONL contract keeps questions/context bounded and returns
literal evidence, actual token/call counts and finite failures. It is a trusted
launcher, without an additional HTTP or identity boundary.

```sh
# Use the existing requirements-generator.lock setup and pinned local assets.
.venv-generator/bin/python -m enterprise_ai_lab.edge demo \
  --model-dir /path/to/pinned/snapshot --out reports/local/edge.json
python -m enterprise_ai_lab.edge verify-report --report reports/local/edge.json
python -W error::ResourceWarning -m unittest discover -s tests/edge -v
```

The fixed fictional protocol keeps the first request after each model load and
six warm requests for each of 1 and 4 PyTorch intraop threads. The recorded run
uses an installed 0.13.0 wheel outside the checkout under macOS network denial.
Request timing includes formatting, tokenization, generation and validation;
whole-process peak RSS is kept separately from latency. Format failures remain
in the record. These small sequential samples establish neither answer quality,
tail latency, throughput nor a general hardware advantage. OS caches were not
cold-controlled; NVIDIA/production serving remains planned.

The 17 model-free tests exercise strict reports, real pipe backpressure, malformed
frames, startup failures and process cleanup. CI recomputes recorded metrics;
it does not load or time a neural model. Actual inference needs Python 3.12+ and
the optional exact generator runtime; contract checks work on Python 3.10+.

[Protocol, tradeoffs and exercises](docs/edge-inference.md) ·
[Recorded CPU run](reports/edge-v1)

## Review a configuration change and keep its receipt

The [local admin study](docs/admin-portal.md) uses a separate real Code + PKCE
identity, tenant-scoped administrator/auditor permissions and a browser desk for
two fictional work-order view settings. The actual source query still filters
the signed tenant AND row roles before applying the configured limit.

```sh
python -m pip install -r requirements-approval.lock
python -m enterprise_ai_lab.admin demo \
  --identity-dir ../identity-boundary-lab --node node \
  --work-dir .local/admin-demo --out reports/local/admin-demo.json \
  --browser-test --browser-script tests/admin/browser.mjs
```

Use Python3.12+, Node24, the built companion identity, and explicit locked
Chromium setup from the guide. Configuration, audit and exact command receipts
share one SQLite commit. After a lost response, inspect state and retry only the
unchanged command and ID. Rollback creates a new revision. The desk cannot execute
system commands or alter permissions, model settings or a production service.

[Protocol and exercises](docs/admin-portal.md) · [Recorded local run](reports/admin-v1)

## Redact before a local tool and restore in context

The privacy study sends fictional text through a narrow deterministic detector,
a private SQLite token map, an actual local HTTP tool and scoped restoration.
The dedicated OAuth resource requires signed tenant/client identity, operation
scopes and roles. Another client in the same tenant cannot restore a context.

Use Python **3.12+** and the sibling Identity Boundary Lab revision pinned by the
privacy CI job, with **Node 24**:

```sh
python3.13 -m venv .venv-privacy
.venv-privacy/bin/python -m pip install -r requirements-approval.lock
.venv-privacy/bin/python -m pip install --no-deps -e .
(cd ../identity-boundary-lab && npm ci --ignore-scripts && npm run build)
.venv-privacy/bin/python -m enterprise_ai_lab.privacy_proxy demo \
  --identity-dir ../identity-boundary-lab --node node \
  --work-dir .local/privacy-demo --out reports/local/privacy-demo.json
.venv-privacy/bin/python -m enterprise_ai_lab.privacy_proxy evaluate \
  --out reports/local/privacy-evaluation.json
.venv-privacy/bin/python -m enterprise_ai_lab.privacy_proxy verify-report \
  reports/local/privacy-evaluation.json
```

The actual demo records 25 HTTP cases, denied cross-client restoration, resource
restart, real SQLite contention, context expiry and a scan of resource logs for
its known sensitive substrings and bearer tokens. The tool receives no bearer
credential. Unknown response placeholders withhold the whole restoration.

Detection covers narrow email, `+1` phone and Luhn-valid card-like formats. The
frozen **36-case development fixture** retains 28 exact matches, one false
positive and one miss among supported kinds; including names/addresses and other
unsupported formats leaves **12 misses out of 40 annotations**. A fictional name
deliberately passes through the demo. This is not universal PII protection.
The local map is plaintext; expiry is logical deletion, not secure erasure.

Read the [protocol, tradeoffs and exercises](docs/privacy-proxy.md) and
[complete recorded results](reports/privacy-v1). No model, paid service or
external provider is used by this study.

## Run the first experiment

Python **3.10+**, with no runtime dependencies, credentials, model download or API
calls. From the repository root, on macOS/Linux:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m enterprise_ai_lab demo --output reports/local/readme-demo --seed 42 --k 3
```

Actual CLI output:

```json
{"output": "reports/local/readme-demo", "safe_query_leakage": 0.0, "unsafe_query_leakage": 0.9}
```

The experiment creates **50 synthetic documents and 100 queries**, runs the same
lexical ranker under both policies, and writes complete result bundles:

```text
reports/local/readme-demo/
├── report.json
├── documents.jsonl
├── cases.jsonl
├── predictions.jsonl
├── results.jsonl
├── manifest.json
└── unsafe_global_baseline/
    └── ...the same bundle structure
```

The manifest records file hashes. The committed [filtered report](reports/reference/report.json),
[unsafe report](reports/reference/unsafe_global_baseline/report.json) and
[per-case evidence](reports/reference/results.jsonl) use the same seed and `k`.

## Read the result, including the failure

| Measured property · synthetic fixture | Tenant/role prefilter | Unsafe global ranker |
| --- | ---: | ---: |
| Queries returning unauthorized IDs | 0 / 100 | 90 / 100 |
| Answerable queries with a relevant result | 50 / 50 | 50 / 50 |
| Correct refusals on unanswerable queries | 40 / 50 | 10 / 50 |

The filtered system still fails to refuse **10 unanswerable queries**. Those
queries share words with permitted documents that do not answer the question.
Permission and relevance are separate checks; returning an allowed document
does not establish that it is useful evidence.

```mermaid
flowchart LR
    Q[Query + fixture identity] --> A[Tenant AND role filter]
    D[Synthetic documents] --> A
    A --> R[Lexical ranking]
    R --> P[Retrieved IDs + refusal flag]
    P --> E[Compare with authorized relevance labels]
    E --> B[Per-case results + metrics + hashes]
```

The evaluator checks retrieval hit rate, macro recall, query/document leakage and
abstention. It rejects missing or duplicate cases, unknown document IDs, invalid
relevance labels, nonfinite scores and malformed predictions. Empty retrieval
has an explicit representation and denominator.

## Bring another retriever's predictions

Use the included JSONL example files to exercise the replay interface:

```sh
.venv/bin/python -m enterprise_ai_lab evaluate \
  --documents examples/documents.jsonl \
  --cases examples/cases.jsonl \
  --predictions examples/predictions.jsonl \
  --output reports/local/readme-replay
```

Replace only the predictions file with another system's output, following the
[data contract](docs/data-contract.md). Keep source documents, case IDs and
relevance labels fixed when comparing systems. Do not commit private inputs or
outputs.

## Continue the sequence

| Study | Runnable companion | Boundary to examine |
| --- | --- | --- |
| 2 · Discovery to scope | [BriefTrace](https://github.com/edsonsims/brieftrace#run-the-story): Python compiler, exact source spans, SQLite revisions and a local HTTP API | Missing facts stay questions; proposed criteria require review. No model extraction or commercial approval is implied. |
| 3 · Identity and authorization | [Identity Boundary Lab](https://github.com/edsonsims/identity-boundary-lab#run-the-experiment): TypeScript, actual OAuth issuer, JWT verification and opaque-token introspection | Compare revocation, issuer availability and tenant/role checks. A separate local Code + PKCE issuer now supplies browser review accounts; external SSO, SAML and SCIM remain planned. |

Identity Boundary Lab now supplies the issuer for the connected RAG experiment
above. BriefTrace remains a separate discovery/compiler study. Each repository
keeps its own setup, demo, tests and design decisions.

## Test and study it

```sh
.venv/bin/python -m unittest discover -s tests -v
```

The [first study](docs/study-01.md) records the hypothesis, fixture categories,
metric definitions, verification and exercises. Try changing `k`, explain one
failure from the JSONL, or build a counterexample to the refusal heuristic.
Owner study and human review remain unclaimed until recorded.

## What these numbers establish

The original lexical study is a controlled engineering experiment with repeated
synthetic templates. Its scores do not establish production security, model accuracy, generalization,
compliance or customer ROI. The Wilson values in JSON are descriptive binomial
calculations; the correlated templates do not support population confidence claims.

In the original lexical experiment, identities and ACLs remain trusted fixture
inputs. The connected RAG study above adds actual token validation, dense-vector
retrieval and an optional local generator as a separate path. The PDF/OCR path
adds real public sources and source spans; live entitlement synchronization and
enterprise-system connectors remain future work. The unsafe
global baseline is confined to the offline evaluator.

Security filtering is an established pattern. Microsoft's
[security trimming documentation](https://learn.microsoft.com/en-us/azure/search/search-security-trimming-for-azure-search)
describes principal-based filtering and its distinction from authentication.
This repository studies that boundary and claims no new retrieval method.

[Roadmap](ROADMAP.md) · [Contribution guide](CONTRIBUTING.md) ·
[MIT license](LICENSE)
