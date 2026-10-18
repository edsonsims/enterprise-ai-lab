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

Local Python3.13.1/macOS arm64 verification passes **112 tests and94 subtests**
with actual embedding and cross-repository integration enabled. Identity Boundary
Lab separately passes81 Node24.19.0 tests. Controlled negative fixtures/fault
injection are distinguished from actual provider/model calls. The original14-test
dependency-free experiment and reports remain separate and reproducible.

The actual optional Qwen observations retain malformed-output rejections and an
answer whose literal quote does not supply a useful explanation. They are small
synthetic observations, not an answer-quality benchmark or semantic safety claim.
No owner human review/study, production deployment or customer validation is
claimed. Model files and private bearer-token IPC are excluded from Git; published
prompts/results contain only the authored synthetic corpus.
