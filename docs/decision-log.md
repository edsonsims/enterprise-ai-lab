# Decision log

## 2026-10-22: preserve usage and freeze the reconciliation result

**Question.** Can a local ledger reconcile fractional fictional charges without
cross-tenant totals, duplicate effects or a statement whose answer changes later?

**Decision.** Use authored usage and fictional versioned rates, exact decimal-text
amounts, tenant-local event IDs/ordinals and append-only SQLite receipts. A fresh
bounded Decimal context computes exact line charges; aggregate-first half-even
settlement is the only rounding boundary. Reconciliation binds its input, rate
card and snapshot to a durable tenant/statement ID. Same input replays that frozen
result; a new statement ID observes later usage. This avoids silently repricing or
reinterpreting prior receipts.

**Tradeoffs.** Full selected-tenant integrity checks cost reads and CPU. Immutable
statement receipts consume bounded storage and `BEGIN IMMEDIATE` serializes writers.
There is no provider collection, authenticated API, credit/tax/FX system or billing
execution. Launcher profiles and one trusted host remain explicit assumptions.

**Storage lesson.** Admission rechecks captured inode/mode/schema without raw
database descriptor closes. Closing a separate raw descriptor can cancel a
process's SQLite POSIX locks; actual foreign-process contention tests preserve
both component and unmanaged SQLite transaction locks. Writer page limits are
applied on every verified connection, rather than assumed persistent after init.

**Evidence.** Real CLI processes exercise restart, duplicate writers, conflict and
a child killed after commit before stdout. Independently authored statements keep
missing/extra/amount/currency/rounding failures visible. These are fictional local
correctness cases, not measured provider prices, customer savings, ROI or owner
study. See [the accounting contract](cost-attribution.md).

## 2026-10-21: fail over a read without bypassing a refusal

**Question.** What does a caller know after a local MCP connection times out or
closes, and which observation permits another read?

**Decision.** Bind one trusted profile to at most two fixed local replicas of the
existing synthetic fixture. Validate full source content and grants, seal byte
hashes and compare successful wire results with the exact fixture-authorized
contract. Only an official SDK timeout or verified transport closure may advance
once. All tool refusals, audit failures, malformed output, provenance drift and
caller cancellation are terminal. No general SQL/action tool or new dependency
is introduced.

**Budget.** Keep an absolute total work/acceptance deadline, capped per-attempt
work and at most two attempts. Recompute remaining time after mandatory cleanup;
do not grant another full total. The official transport owns reaping. Only a
separately observed owned child may be killed when cleanup must stop a stalled
process. Work deadlines and measured whole-call time are distinct; no hard
real-time exit guarantee is made.

**Evidence and tradeoff.** Real local processes lose responses after committed
reads/audit outcomes, die after a durable intent, stall, disconnect, hit actual
SQLite locks and recover. Refusals and native cancellation never reach the
secondary. Additional read attempts can leave duplicate audit records; an
unfinished intent is preserved. This favors inspectable refusal over hiding
failure and costs admission/startup, strict fixture matching and cleanup time.
The [separate chaos report](../reports/chaos-v1) retains timings and failures.
This same-host immutable-data experiment is not replication, an atomic
source/audit transaction, exactly-once effects or production availability.


## 2026-10-19: preserve finite evidence before retrying an unconfirmed review

**Problem.** An intermittent browser-review CI 503 lost its HTTP category and
backend failure stage during temporary-directory cleanup. Identical relevant
source and later green runs do not identify its cause. Blind retries or longer
timeouts could hide an already accepted workflow rather than explain it.

**Decision.** Keep generic authenticated responses and unchanged request bodies,
deadlines and admission limits. Add an opt-in bounded local diagnostic sink with
fixed operation/stage/category/status enums, never exception text or private
identifiers. Keep diagnostics separate from the durable audit. Best-effort sink
failure must not change request behavior. Validate existing bytes before append,
and reserialize only a strict bounded failure snapshot for CI upload. A private
work directory is never an artifact source.

**Tradeoff.** A finite vocabulary and no request identity protect document/account
content but sacrifice message detail, timing and exact cross-request correlation.
The 64 KiB sink can drop events, so an absent/truncated snapshot is explicit and
even a complete readable snapshot does not establish complete event coverage.
Local filesystem administration remains trusted. This is observability for a
local study, not production telemetry or a durable audit replacement.

**Evidence.** A separate deliberately absent-worker experiment uses actual
Temporal, the signed browser-account protocol, public native/OCR candidate and
existing vector resource. Independent history proves an accepted running start
despite HTTP 503. Once a real worker task completes, one identical explicit POST
returns pending on the same execution with zero publication effects or visible
RAG citations. Exact SDK status and timings remain recorded; no internal cause
is inferred from the configured timeout. Controlled HTTP/sink tests and actual
Chromium regression are separate. The historical CI cause remains unknown.
See [review diagnostics](../reports/review-diagnostics-v1).

## 2026-10-19: expose named reads before a general database tool

**Question.** Can an actual MCP client read useful records while tenant, row
roles, source immutability and durable audit remain enforced outside the model?

**Decision.** Use official MCP2 stdio and an authored synthetic SQLite work-order
fixture. The trusted local launcher supplies immutable profiles. Expose two
reader tools and one analyst diagnostic tool, with strict service validation,
parameterized SQL and tenant AND row-role filtering before page limits. There
is no general SQL tool, model or caller-controlled tenant/role field. This
local launcher boundary is not remote authentication.

**Protocol decision.** Pin the actual SDK2.3 artifact and use its public low-level
Server callbacks. This retains rejected arguments for a durable audit instead
of decorator coercion dropping extra fields. The real official Client discovers
the modern protocol and separately exercises the legacy initialize handshake;
both versions are recorded from the wire rather than inferred from the SDK.

**Storage decision.** Open the source read-only with an authorizer and bounded
busy/progress handling. Commit audit intent before source access and outcome
before returning. Audit failure after a read withholds data and preserves an
honest unfinished intent. This costs two durable writes and full-chain checking
per attempt, and it is not a transaction across source and audit. The bounded
local chain detects corruption but has no external anchor against an admin.

**Review finding.** A hardlink from an audit SQLite sidecar to the source could
overwrite the source despite distinct main paths. Independent automated review
reproduced this with disposable real files. Constructor and call admission now
check every existing main/WAL/SHM/journal inode pairing before audit I/O, with
real-file regressions preserving the source hash. The host/filesystem still
remain trusted; this is not a hostile-file sandbox.

**Evidence and limits.** The demo exercises actual separate processes, colliding
tenant IDs, role-filtered multi-page reads, safe errors, restart persistence,
locked databases, corrupt audit and source hash preservation. Separate tests
exercise SDK timeout and cancellation after a durable intent, without claiming
rollback, and verify explicit recovery/process reaping. No Oracle/SAP, remote
OAuth, action tool, new model inference, owner study or production claim exists.

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


## 2026-10-18: a signed-in account can inspect the exact publication candidate

**Question.** Can a person inspect the public source and extracted words, decide
the immutable publication request, and later trace its durable acceptance without
turning an approver role into unrestricted document access?

**Identity decision.** Add a separate local `oidc-provider` browser issuer using
Authorization Code + mandatory PKCE S256. Keep client-credentials service
experiments unchanged. The review token has its own audience and user-kind claim;
the signed account subject, not the shared `review-portal` client ID, becomes the
workflow actor. Bind the callback to state, browser cookie, verifier, nonce,
signatures, audience and recent ID-token authentication time. Store bearer tokens
in portal memory behind opaque HttpOnly cookies, revalidate each protected
request and require same-origin JSON plus session CSRF on mutations. Tokens
default to five minutes and are bounded to ten; restart or expiry requires login.
This is a local fixture, not external federation or production password storage.

**Document decision.** Reuse the immutable candidate and existing publication
workflow. Derive the tenant only from the verified user. Show only documents
whose allowed roles intersect that account's roles; withhold even titles, URLs,
PDF hashes and OCR geometry for unreadable pages. Require complete-batch reading
permission before either approval or rejection. Keep self-approval prohibited.
An independent analyst reviewer can decide a mixed reader/analyst batch; a
reader-only approver can inspect its native page but cannot decide blindly.

**Preview decision.** Serve one individually authorized page as a labelled PDF
derivative, never the whole original as a convenience download. Verify both
original and processed source snapshots against the trusted manifest and
immutable candidate on each preview. Keep preview, original and artifact hashes
distinct. Display canonical text, exact Unicode chunk spans, source URLs/pages
and ACLs beside the page. This is extra local rendering/storage work and a
bounded public-fixture viewer, not a hostile-upload sandbox.

**Audit decision.** Append small local SQLite events without passwords, tokens
or document bodies. Hash-link and validate the complete chain before filtering
its permitted tenant/document view. Persist request/decision intents before
Temporal calls, then reconcile accepted immutable receipts on later reads after
a process gap. Unknown service-side decisions stay labelled `service_or_unknown`.
Do not claim an atomic transaction across audit, workflow, publication ledger and
vectors. The host administrator can replace the database and all hashes; the
chain is not external tamper-proof evidence. Approval and publication remain
separate events.

**Evidence and limits.** Controlled tests isolate negative claims, callback/CSRF
checks, audit corruption and receipt gaps. The process demo uses actual local
OIDC forms/cookies, public PDF extraction/OCR, Temporal, MiniLM and Qdrant; optional
Chromium performs rendered UI actions separately. Scripted fixture accounts do
not establish that the owner or any person read or approved a source. The local
Temporal gRPC administrator, source files and trusted grants remain outside the
browser authorization boundary. No production identity, generalized OCR
accuracy, answer-quality improvement or customer outcome is claimed.

## 2026-10-20: scope restoration to the client that created the map

**Question.** Can selected substrings stay out of a downstream tool's request
without turning every placeholder into a reusable cross-tenant recovery key?

**Decision.** Keep narrow deterministic detection separate from storage and
authorization. The actual local issuer grants a dedicated privacy JWT audience
and role-bounded scopes; the resource enforces scope AND role. Each occurrence
gets a fresh random token in a generated context. SQLite binds every restoration
to the original signed tenant AND client AND unexpired context. Persistent ID
tombstones prevent reuse after logical expiry. Unknown or copied placeholders
withhold the whole result, and no bearer/identity headers reach the fixed local
HTTP processor. Recheck the short-lived credential after downstream work.

**Tradeoff.** Strict per-client scope prevents an implicit reviewer/agent handoff;
an explicit delegation protocol would be a separate study. Random per-occurrence
tokens avoid a global reusable dictionary, but downstream work can lose or repeat
them. Restoration proves mapping provenance, not answer semantics. Finite audit
and tombstone budgets eventually require an explicit fresh private vault. The
stored substrings are plaintext and expiry is logical deletion, not secure
erasure. Redaction retry creates a new context; HTTP processing and SQLite do
not share a transaction.

**Evidence.** The [frozen process run](../reports/privacy-v1) exercises actual
issuer/JWT/HTTP boundaries, three resource processes, restart, a real SQLite
lock, expiry, metadata-only audit and a finite resource-log scan. The separate
36-case fictional development evaluation retains a quoted-mailbox miss,
context-dependent public-contact false positive and eleven unsupported-format
misses. Supported-kind precision/recall are 28/29; all-annotation recall is 28/40.
There is no held-out generalization or comprehensive privacy claim. The local
processor deliberately forwards a fictional name unchanged and calls no model.

**Review repair.** A fresh work directory initially did not protect an existing
report path. Report creation now refuses existing paths and uses exclusive
creation, with a regression before any child service starts. Actual slow-peer
checks also established that the 3-second HTTP budget is checked between reads;
the 2-second I/O timeout can extend elapsed time. The documented bound preserves
that observation instead of claiming a hard 3-second cutoff.


## 2026-10-23: bind modeled benefit to one usage snapshot

**Question.** Can an activity dashboard expose its financial assumptions without
turning request counts into completed work or hypothetical labor into measured ROI?

**Decision.** Read the existing tenant-authorized cost ledger once, retain its
watermark, full-receipt selection digest and compact event references, then derive
both equal-duration activity windows from that in-memory snapshot. Keep all benefit
parameters explicitly authored. Use exact rational arithmetic for the27 scenarios,
including divisions by60, with half-even rounding at the display boundary only.
Zero denominators remain undefined. The self-contained private HTML is an export,
not a new authenticated service; copied files carry no live access policy.

**Tradeoff.** A few declared activity flags are inspectable but establish neither
customer health nor prediction. Usage request IDs do not establish successful or
completed tasks. Scenario extrema bound only the declared inputs, not real outcomes
or statistical confidence. Compact report references cannot reconstruct the full-row
digest without the original ledger receipts. Interrupted multi-file exports can
leave an incomplete new directory; use a fresh path rather than overwriting history.

**Evidence.** The [recorded CLI story](../reports/health-v1) preserves15 real processes:
fictional tenant collisions, observer refusal, identical repeated snapshots, a later
append and immutable previous exports. Focused tests check exact arithmetic, hostile
Decimal contexts, single-read consistency, malformed input/storage and private files.
Actual Chromium checks render desktop/mobile and keyboard-scrollable tables. A local
0.11.0 wheel reproduces the scenario outside the checkout. No actual customer outcome,
owner study, human review, model call or registry release is inferred.
