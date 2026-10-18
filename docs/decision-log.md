# Decision log

## 2026-10-17: one connected lab, evaluation first

**Context.** The goal is a balanced portfolio for senior software engineering and
applied AI/research engineering. The source list contains interdependent enterprise
components, many of which require identity systems, special hardware, real data, or
deployment environments.

**Decision.** Start with a dependency-light offline evaluation harness and a
permission-aware lexical retrieval fixture. Grow a connected lab rather than
declaring 15 complete projects. Keep core interfaces small enough to extract useful
standalone tools later.

**Reasoning.** The first experiment can run without credentials, customer data,
cloud spend, or special hardware. It establishes data contracts and negative tests
that later integrations must pass. The initial retrieval mechanism is deliberately
simple so permission and measurement behavior can be inspected directly.

**Alternatives.** Starting with a full identity deployment gives a stronger live
integration but delays measurement. Starting with 15 repositories makes scope easy
to browse but creates maintenance overhead before the components exist.

**Limits.** Lexical retrieval and fixture identities do not establish LLM answer
quality or production authorization. The synthetic cases are designed tests, not
an independent sample of customer traffic. AI-assisted implementation and automated
review are disclosed separately from the owner's personal study.


## 2026-10-17: connect identity before retrieved context

**Question.** Can an actual OAuth identity reach a vector-backed answer resource
without turning caller-supplied tenant/role fields into authorization?

**Decision.** Keep Python as the resource server. Verify the original issuer JWT
against an explicit loopback discovery/JWKS snapshot and a dedicated RAG audience.
Derive Qdrant's mandatory tenant AND role filter from the verified principal,
before top-k. Revalidate returned payloads before giving context to a generator.
The Node issuer and Python resource run as separate actual HTTP processes.

**Storage/model choice.** Qdrant local mode gives a persistent real vector backend
without Docker/cloud setup. MiniLM ONNX gives actual CPU dense embeddings, with
explicit download and hash-pinned offline assets. Local mode exercises embedded
storage and filtering, not a remote/distributed Qdrant deployment or HNSW scale.

**Answer choice.** An extractive reference makes the boundary easy to inspect.
The optional official Qwen3-0.6B adapter demonstrates actual CPU decoding with
strict JSON/evidence checks. Format failures and semantically weak answers remain
in the report; no silent fallback turns them into apparent neural success.
Exact quotation checks do not establish entailment or answer quality.

**Failure finding.** Qdrant's local persistence commits points individually.
An interrupted ACL/reindex operation can leave a partial state. All local
mutations therefore write a pending marker before changing points; a failed or
interrupted update blocks current queries and subsequent reopen. Recovery rebuilds
a fresh index from trusted inputs. Tests inject actual partially committed writes.
This favors denial over stale permissions and makes no power-loss guarantee.

**Evidence/limits.** The connected fixture has six authored documents/eight
chunks. Tests use actual HTTP, signatures, embeddings and persisted vectors;
negative cases also use controlled signed fixtures and fault injection. Offline
JWTs retain roles until expiry and use a startup key snapshot. Corpus permissions
are trusted authored metadata; live permission synchronization and production
identity lifecycle remain outside this milestone. Owner study is not claimed.

## 2026-10-17: keep PDF evidence and permissions together

**Question.** Can public PDF pages enter the existing authenticated RAG without
losing their origin or turning document contents into permission metadata?

**Decision.** Use native `pypdf` text when it is present. Render empty native
pages with PDFium and execute Tesseract; allow an explicit per-page OCR override
for unsuitable embedded text. Always OCR would add time and recognition errors
to text that already exists. Native extraction can still have poor reading order
or incomplete text, so neither path is a layout-fidelity guarantee.

**Provenance.** Keep unchanged public originals and a labelled image-only
derivative as separate artifacts. Retain original and processed file hashes,
their physical page numbers, the extraction method, the full canonical page-text
hash and exact chunk spans. OCR adds word spans, rendered-pixel boxes and raw
confidence diagnostics. Verify original page bounds; identical original/artifact
hashes cannot claim different page numbers. A derivative's source-page mapping
is a trusted operator assertion with a recorded build procedure, not an OCR
inference. Exact quoting establishes a location, not semantic answer quality.

**Permissions and identity.** A trusted local manifest supplies tenant and role
grants. PDF metadata, text, links and model output cannot grant access. Stable
document IDs include tenant, public source ID and original page, so re-ingestion
replaces the same tenant's content/ACLs while identical bytes in another tenant
have a distinct ID. Omitting a page is not a deletion request. The query still
derives identity only from a verified JWT and filters tenant AND role before
vector top-k.

**Failure tradeoff.** Extract all selected pages before a single guarded write;
bound file size, pages, decoded content, pixels, words and process time. Preserve
the existing pending marker that refuses queries/reopen after an interrupted
index mutation. A fresh schema-2 index is required for source-bearing payloads;
rebuilding is more explicit than guessing how to migrate partially trusted
payloads. The operator pays a rebuild/availability cost rather than receiving
silently incomplete permissions or citations. This is local process isolation
and fail-closed recovery, not an upload sandbox or distributed transaction.

**Evidence/limits.** The public demo selects one native NIST page and one clean
rasterized NIST page, reused with authored grants in two tenants. It runs the
actual issuer, HTTP resource, OCR, MiniLM embeddings and persisted Qdrant with an
extractive answer baseline. Negative PDFs, controlled vectors and injected
failures remain identified as test fixtures. Difficult-scan accuracy, broad OCR
error analysis, live entitlement synchronization and owner human review remain
outside this milestone.

## 2026-10-18: bind a durable decision to the action being reviewed

**Question.** Can an approval wait survive process failure without losing the
reviewed payload or duplicating the resulting action?

**Decision.** Use an actual Temporal workflow with a bounded, immutable snapshot.
Hash the fixed local-note action, full payload, tenant, requester, request ID and
deadline together. Derive decision actors from a dedicated verified OAuth
audience; require an independent approver. Use a validated synchronous Update so
the caller receives a durable decision receipt. A workflow timer limits decision
acceptance, while an already approved activity may finish after that deadline.

**Effect boundary.** Publish only to a local SQLite note sink. The activity
commits a unique tenant/request key with the full snapshot/decision content and
checks exact equality on redelivery. A killed worker can lose the acknowledgement
after commit; a retry then returns the same local effect receipt. This handles a
specific at-least-once delivery gap without claiming a distributed transaction
or exactly-once external messaging. Approval remains distinct from publication.

**Idempotency finding.** Temporal deduplicates an Update ID before invoking its
validator. Hash the entire server-derived decision command into the Update ID
and check the returned receipt against that command. A command ID alone could
return an old acceptance when the caller supplies altered content. Identical
retries retain their receipt; altered requests and terminal decisions conflict.

**Alternatives and cost.** A SQLite-only queue would need less runtime setup,
but would leave durable waits, timers and replay semantics to this repository.
Temporal provides those mechanisms and a stronger recovery experiment, with an
additional server, SDK and code-versioning burden. The pinned native development
server keeps the demo local and offline after explicit bootstrap; it does not
exercise a distributed cluster or production operations.

**Evidence and limits.** Actual server/API restart, a separate worker killed
after sink commit and captured-history replay are distinct checks. Controlled
negative inputs and fault hooks remain labelled. The reviewer is a scripted
service identity, not a logged-in human. The local unauthenticated gRPC endpoint
trusts developer/admin callers; HTTP JWT validation does not protect that path.
Human review UI, production authorization, history/version migrations, retention
and external actions remain planned. No owner study or live customer use is
claimed.

## 2026-10-18: approve the same content that retrieval will serve

**Question.** Can the durable decision gate publish actual native/OCR documents
without losing their tenant, role grants or source spans, and without a delayed
approval restoring an obsolete revision?

**Decision.** Add a separately versioned document-publication workflow rather
than changing the replay behavior of the local-note workflow. Stage immutable
candidate documents outside search. Hash their complete text, title, IDs, ACLs
and provenance, then bind that digest to the authenticated requester, tenant,
deadline, action and expected published revision. Keep the approval and RAG JWT
audiences separate. Only an independent accepted approval schedules publication.

**Storage ownership.** One resource process owns the embedded Qdrant store and
shares its lock between queries and publication activities. A SQLite ledger
keeps tenant revision state, complete effect identity and a durable pending
journal. Do not open the same Qdrant local directory in a separate worker.
Startup binds ledger/index ownership; every query checks live vector payloads
against the committed inventory and its immutable approved candidates. This is
more local integrity work than a remote database
transaction, but keeps the runnable experiment offline and inspectable.

**Redelivery and concurrency.** Check an exact already committed effect before
the revision comparison. It returns its saved receipt without writing vectors,
even if a later approval has changed the live revision. New publications compare
their expected tenant corpus revision under the owner lock; stale approvals
conflict. This conservative CAS serializes unrelated concurrent batches in one
tenant, while other tenants keep separate revisions. A batch replaces named
documents and retains omitted pages. A
pending replacement does not hide or mutate existing approved content. An
approved decision and a successfully published effect remain distinct states.

**Partial failure.** Commit the pending journal before changing vectors and
finish the effect ledger afterward. Validate and embed the entire batch before
creating that journal: character bounds alone do not establish that a document
fits MiniLM's tokenizer. A preparation error must leave the previous approved
corpus readable. Apply the already prepared batch once under the owner lock.
Preserve the existing vector mutation
marker. An unfinished journal blocks reads; only its exact operation can resume
when the vector inventory is either the old committed view or the complete
approved next view and no vector mutation marker exists. A partial or unexplained
mutation requires a fresh deployment from trusted approved inputs instead of
deleting the fence. This
trades availability for refusing ambiguous content/ACLs. It is not an atomic
transaction across Temporal, SQLite and Qdrant or an exactly-once external
delivery guarantee.

**Scope.** The authenticated gate applies to the connected publication resource,
which exposes no direct ingest/ACL management endpoint. The standalone trusted
`rag ingest` command remains a separate study and does not acquire approval
semantics. Local administrators and the unauthenticated development Temporal
gRPC endpoint remain trusted; this is not production administration security.

**Evidence and limits.** The connected public fixture reuses the unchanged NIST
native page and labelled raster OCR page with four authored tenant/page grants.
Citation metadata preserves both original and processed artifacts, physical
pages, canonical text hashes, exact Unicode spans and OCR word geometry. The
reviewer is scripted, the answer step extractive, and controlled failures are
separate from actual service/model/OCR calls. Human review, owner study, difficult
scan accuracy and independent answer-quality measurements remain unclaimed.

**Review finding.** An initially accepted 400-character CJK candidate could
exceed the real embedding model's 256-token limit after the outer journal had
already been created. No vector write had occurred, but the previous approved
corpus became unnecessarily fenced. Batch preparation now happens before the
journal, with a regression using the actual pinned tokenizer/model. The existing
vector mutation marker still guards the later write phase; no marker is cleared
to repair a failed publication.
