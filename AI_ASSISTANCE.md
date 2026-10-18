# AI assistance and ownership

The initial code, tests, and documentation in this repository were developed with
Codex at the owner's request. Automated agents also reviewed and exercised the
implementation. This is a record of AI-assisted engineering and an invitation to
study and improve the work.

The repository does not claim that the owner manually wrote every line, personally
reviewed every result, completed the study exercises, or deployed these systems for
a real customer. Owner review and study are recorded only when explicitly reported
by the owner. Tests and automated review are separate evidence.

Upstream projects set their own contribution policies. This repository's permission
to use AI does not override those policies. AI-assisted upstream submissions should
disclose material assistance and include commands and results that another reviewer
can reproduce.

The README visual refresh and original SVG illustrations were also drafted with Codex. The artwork is illustrative, not a screenshot or live system. Runnable README examples and their selected output fields were checked against local CLI/HTTP runs; they do not imply owner review, production use or new model-quality evidence.


The connected RAG milestone was also developed and reviewed with Codex. It uses
an actual Node OAuth issuer, Python JWT verification, local MiniLM ONNX embeddings,
persistent Qdrant local mode and an optional actual Qwen CPU generator. Separate
automated agents reviewed authentication/admission and vector/persistence behavior,
including partial ACL writes and generator context boundaries.

The original connected milestone's Python 3.13.1/macOS arm64 verification passed
**112 tests and 94 subtests** with actual embedding and cross-repository
integration enabled. Identity Boundary Lab separately passed 81 Node 24.19.0
tests. Those are that milestone's recorded counts. Controlled negative fixtures/fault
injection are distinguished from actual provider/model calls. The original14-test
dependency-free experiment and reports remain separate and reproducible.

The actual optional Qwen observations retain malformed-output rejections and an
answer whose literal quote does not supply a useful explanation. They are small
synthetic observations, not an answer-quality benchmark or semantic safety claim.
No owner human review/study, production deployment or customer validation is
claimed. Model files and private bearer-token IPC are excluded from Git. The
`reports/rag-v1` prompts/results contain the authored synthetic corpus.

The PDF/OCR extension was also implemented and reviewed with Codex. It uses real
`pypdf` extraction, PDFium rendering, the installed Tesseract executable, exact
page-text spans and the existing authenticated vector RAG. Automated agents
independently reviewed extraction boundaries, public source rights, permission
propagation and actual HTTP integration. Controlled negative PDFs and injected
failures are distinguished from the unchanged public originals and actual OCR.

The public demo selects two NIST pages. Its single image-only OCR input is a
labelled lab-created derivative, with a separate hash and explicit original-page
mapping. Tenant/role assignments are authored test policy. The publication text
is public source material, not synthetic customer data; NIST endorsement is not
claimed. The [public run](reports/pdf-v1/demo.json), [source manifest](public-data/pdf-v1/manifest.json)
and [PDF/OCR design](docs/pdf-ingestion.md) retain those distinctions. This small
fixture does not establish general OCR accuracy, production security, semantic
answer quality, owner human review or personal completion of the study.

Final local PDF/OCR verification on Python 3.13.1/macOS arm64 passed **208 tests
and 94 subtests**, with actual Tesseract, MiniLM ONNX, persisted Qdrant and the
Node 24.19.0 issuer enabled. The original Python 3.10 dependency-free suite also
passed its 14 tests and reproduced the original reference reports without a diff.
The public PDF demo records nine HTTP cases, four page grants and 28 chunks;
re-ingestion preserves 28. The native-layer/OCR diagnostic preserves both streams
and differences; its alignment ratio is not OCR accuracy.

Independent automated review found and verified repairs for PDFium resource
handling, sentence-boundary splits, equal-byte aliases with false source-page
mappings, original-source replacement between hash verification and extraction,
non-regular manifest inputs and malformed stored provenance. These are tested
engineering boundaries, not a general security audit or human review.

The durable approval extension was developed with Codex on 2026-10-18. Automated
agents separately implemented the Temporal core/bootstrap and reviewed the
OAuth/HTTP boundary. Core verification used real Temporal Updates, durable timers,
activity retries and SDK history replay; the process demo separately exercises
persistent server/API restart and a worker killed after a local effect commits.
Controlled archive fixtures, forged credentials/claims and injected failures are
identified separately from actual issuer/server calls.

The request and review accounts are authored client-credentials fixtures. A
scripted reviewer accepting a note is **not a person's approval**, owner review,
browser sign-in or completion of the study. The note is an authored local demo
artifact; nothing is sent to an external service. The approved snapshot and
effect receipt describe this bounded local operation, not customer acceptance,
business impact or production authorization.

The [recorded approval run](reports/approval-v1/demo.json) and
[approval design](docs/durable-approval.md) retain process/fault observations,
idempotency limits and the trusted development gRPC boundary. Raw JWTs are not
included in workflow history, notification rows or public reports. No new model
evaluation or human study is claimed by this milestone.

Final combined local verification on Python 3.13.1/macOS arm64 passed **289 tests
and 167 subtests** with RAG/PDF and approval integrations enabled. This includes
81 approval tests and their 73 subtests. The recorded approval process demo used
Python 3.12.14 and retains 29 HTTP observations, five actual exported/replayed
histories, a worker kill after local commit before acknowledgement, and the
subsequent Temporal activity attempt 2 with one completed Alpha effect.

The initial combined test setup omitted the pytest asyncio plugin, so the actual
async Temporal test could not run. The verification lock now includes the exact
`pytest-asyncio==1.4.0` dependency; the complete combined suite subsequently
passed. Original reference files remain byte-identical. Identity Boundary Lab
separately passed 89 Node 24.19.0 tests; its approval scopes are role-bounded
provider grants, and the resource still checks role and scope independently.

The connected approval-to-RAG publication extension was also developed with
Codex at the owner's direct request on 2026-10-18. Separate automated agents
implemented the immutable publication contract/workflow/API and the staging,
revision/effect ledger and vector-owner boundary. An independent automated
review examined provenance, tenant/role isolation, stale revision handling,
redelivery and partial-write recovery. Automated review is not human approval.

This extension reuses the actual public native/OCR PDFs, local OAuth issuer,
Temporal runtime, MiniLM ONNX embeddings and Qdrant local store. A candidate's
hash covers complete text, ACLs and source provenance; the workflow binds that
candidate to the requested publication and expected revision. Its scripted
client-credentials approver is not a logged-in person and does not establish
that the owner read or approved the source document. The extractive answer
baseline adds no new neural model-quality claim.

The [connected publication guide](docs/approved-publication.md) and
[versioned run evidence](reports/publication-v1) distinguish the enforced
resource path from standalone trusted ingestion, controlled fault injection
from actual service calls, and accepted decisions from completed publication.
Original lexical, RAG, PDF/OCR and local-note approval reports remain separate
and unchanged. No owner human review, study completion, production deployment
or customer outcome is claimed.

Independent automated review also found that a character-bounded CJK candidate
could exceed the actual embedding token limit after the publication journal was
created. This caused an availability failure before any vector mutation. The
publication path now prepares every embedding before that journal and applies
the prepared batch once under the owner lock; an actual MiniLM regression checks
that the previously approved corpus stays readable after rejection. The original
vector pending marker remains in force for incomplete writes. This repair is
separate from the process demo's deliberately lost acknowledgement after a
successful commit.

Review also identified that a query could wait behind a long publication after
its short-lived JWT had been checked. The shared query/publisher lock now admits
queries without waiting; a busy resource returns a sanitized 503 for an explicit
retry. This keeps publication work from introducing a queue that outlives the
authenticated query's admission check.

Final combined local verification on Python 3.13.1/macOS arm64 passed **354 tests
and 247 subtests**, with all RAG/PDF, local-note approval and document-publication
integrations enabled. The 65 new publication tests include actual MiniLM token
limit rejection and the real Temporal/OAuth/HTTP/OCR/vector process demo. Other
cases use controlled vector outputs, gateway doubles and fault injection, as
labelled in their modules. The frozen public run retains 48 HTTP observations,
three publication receipts, six exported/replayed histories and an actual
pre-approval Qdrant inventory of zero points, ending at 28. Original reference
reports and all prior versioned reports are unchanged.


The browser-review extension was developed with Codex assistance at the owner's
direct request on 2026-10-18. Separate automated agents implemented the real local
OIDC user-account issuer, portal UI and browser checks, and account-scoped review,
source previews and audit boundaries. Root integration connects that portal to
the existing Temporal publication workflow and actual RAG services. The browser
uses Authorization Code + PKCE S256; its bearer tokens stay in portal memory,
behind opaque HttpOnly session cookies. The user subject is distinct from the
shared OAuth application client ID, preserving independent-account review.

Identity Boundary Lab commit `70e663c69ad96997c394581fa9df33487d2e3390` passed
100 tests and its original demo on Node 22 and 24 in an actual
[CI run](https://github.com/edsonsims/identity-boundary-lab/actions/runs/39139597620).
Its browser identities use generated private local fixture passwords and a
separate review audience. This does not implement an external identity provider,
production password management, MFA or persistent issuer storage.

The portal checks the signed tenant and each document's reading roles before
returning PDF pages, canonical text/chunks or audit events. Both approval and
rejection require a fully readable immutable batch. Page previews are labelled
single-page derivatives, with their own hashes separate from the unchanged
public NIST originals and the existing raster OCR artifact. Their source rights
and prior experiment reports remain unchanged. The local hash-linked audit
records intents and reconciles durable receipts; it is not a transaction across
all stores or an externally anchored, administrator-proof audit service.

The [review guide](docs/human-review.md) and [versioned evidence](reports/review-v1)
distinguish controlled exchange/gateway/signing fixtures, actual HTTP sign-in and
process integration, and rendered Chromium actions. The completed local process
run records 37 HTTP observations, four one-page previews, four publication
effects, six replayed histories and 85 audit events; Chromium 151.0.7922.34 with
Playwright 1.62.1 passes 11 browser checks. Final combined local verification on
Python 3.13.1/macOS arm64 passed **439 tests and 247 subtests**, including all
actual RAG/PDF, approval, publication and browser-review integrations. The 85 new
review tests include 75 focused boundaries and ten actual process cases. Automated logins/clicks are not proof of owner review, human
approval, personal study or source comprehension. The interface can be used by a
person separately; no such action is claimed here. Production HTTPS, external
SSO, key rotation, live permission synchronization and production gRPC
administration remain unimplemented. No new OCR or answer-quality claim is made.


Actual rendered-browser testing found two login failures that the successful
direct HTTP form test missed. The initial no-referrer policy caused Chromium to
submit `Origin: null`; its self-only CSP form-action also blocked the registered
portal redirect. The separate issuer now uses same-origin referrer policy and
permits only self plus the one statically registered portal origin for that
redirect chain. Exact callback, PKCE, Origin and CSRF checks remain intact; null
Origin is still forbidden. Node regressions assert the policies and refusal, and
the subsequent real Chromium pass verifies login, account-scoped PDF/snippet
review, UI approval/rejection, audit, logout, tenant isolation and a narrow
viewport. This is automated browser evidence, not owner or human approval.


The visual preview uses actual PDFium PNG rendering of the individually permitted
page, with a separately authorized single-page PDF link. Chromium checks decoded
pixels and dimensions; it does not infer visible PDF contents from a successful
download. Independent automated review retained pre-allocation dimension/pixel
bounds, serialized native rendering and cleanup regressions. The original
14-test dependency-free study and byte-identical reference demo still pass; a
local wheel includes the review modules, static UI assets and Temporal pin.
The wheel was built for packaging verification, not uploaded to a registry.
