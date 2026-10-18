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
